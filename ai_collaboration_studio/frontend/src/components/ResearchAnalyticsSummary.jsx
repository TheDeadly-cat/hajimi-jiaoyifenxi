import { Activity, AlertTriangle, BarChart3, ChevronDown, ShieldCheck } from "lucide-react";
import { useId, useMemo, useState } from "react";
import "../styles/research-analytics-summary.css";

const TEXT_LIMIT = 160;

function objectValue(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function arrayValue(value) {
  return Array.isArray(value) ? value : [];
}

function boundedText(value, limit = TEXT_LIMIT) {
  const text = String(value ?? "").trim();
  return text.length <= limit ? text : text.slice(0, limit) + "...";
}

function finiteNumber(value) {
  if (value === null || value === undefined) return null;
  if (typeof value === "string" && !value.trim()) return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function positiveInteger(value) {
  const number = finiteNumber(value);
  return Number.isInteger(number) && number > 0 ? number : 0;
}

function percent(value, digits = 1, range = null) {
  const number = finiteNumber(value);
  if (number === null) return "未记录";
  if (range && (number < range[0] || number > range[1])) return "未记录";
  return number.toFixed(digits) + "%";
}

function displaySymbol(value) {
  return boundedText(value, 40).replace(/^US\./, "") || "标的未知";
}

function baseRateModel(baseRates) {
  const rawRows = arrayValue(baseRates.rows);
  const fiveDayRows = rawRows.filter((row) => (
    row && typeof row === "object" && finiteNumber(row.horizon_days) === 5
  ));
  const candidates = fiveDayRows
    .map((row) => ({
      symbol: displaySymbol(row.symbol),
      sampleCount: positiveInteger(row.sample_count),
      upRate: percent(row.up_base_rate_pct, 1, [0, 100]),
      downRate: percent(row.down_base_rate_pct, 1, [0, 100]),
    }))
    .filter((row) => row.sampleCount > 0 && row.symbol !== "标的未知");

  const symbolCounts = candidates.reduce((counts, row) => {
    counts.set(row.symbol, (counts.get(row.symbol) || 0) + 1);
    return counts;
  }, new Map());
  const rows = candidates.filter((row) => symbolCounts.get(row.symbol) === 1);
  const omittedCount = rawRows.length - rows.length;
  const threshold = finiteNumber(baseRates.threshold_pct);

  return {
    rows,
    omittedCount,
    thresholdLabel: threshold === null ? "阈值未记录" : "±" + Math.abs(threshold).toFixed(1) + "%",
  };
}

function analyticsViewModel(analytics) {
  const source = objectValue(analytics);
  const baseRates = objectValue(source.base_rates);
  const risk = objectValue(source.portfolio_risk);
  const base = baseRateModel(baseRates);
  const riskSampleCount = positiveInteger(risk.sample_count);
  const riskAvailable = riskSampleCount > 0;
  const baseAvailable = base.rows.length > 0;
  const available = riskAvailable || baseAvailable;
  const coverage = riskAvailable && baseAvailable && base.omittedCount === 0
    ? "complete"
    : available
      ? "partial"
      : "empty";

  return {
    available,
    base,
    baseAvailable,
    coverage,
    riskAvailable,
    riskSampleCount,
    riskMetrics: [
      { key: "annualized-volatility", label: "年化波动", value: percent(risk.annualized_volatility_pct) },
      { key: "maximum-drawdown", label: "最大回撤", value: percent(risk.max_drawdown_pct) },
      { key: "historical-var", label: "单日 VaR 95%", value: percent(risk.historical_var_95_1d_pct) },
      { key: "worst-five-day", label: "最差 5 日", value: percent(risk.worst_5d_pct) },
    ],
  };
}

function coverageLabel(view) {
  if (view.coverage === "complete") return "两类统计均可用";
  if (view.coverage === "partial") return "统计覆盖不完整";
  return "历史数据不足";
}

export function ResearchAnalyticsSummary({ evidence }) {
  const analytics = evidence?.research_analytics;
  const view = useMemo(() => analyticsViewModel(analytics), [analytics]);
  const disclosureId = `${useId()}-analytics-disclosure`;
  const [disclosure, setDisclosure] = useState(() => ({
    coverage: view.coverage,
    open: view.available,
  }));
  const expanded = disclosure.coverage === view.coverage
    ? disclosure.open
    : view.available;

  if (!analytics) return null;

  return (
    <details
      className="research-analytics research-analytics-v2"
      data-coverage={view.coverage}
      open={expanded}
      onToggle={(event) => setDisclosure({ coverage: view.coverage, open: event.currentTarget.open })}
    >
      <summary aria-controls={disclosureId} aria-expanded={expanded}>
        <span><Activity size={15} aria-hidden="true" />历史基准与等权模拟风险</span>
        <small className={view.coverage}>{coverageLabel(view)}</small>
        <ChevronDown className="research-analytics-chevron" size={15} aria-hidden="true" />
      </summary>

      <div className="research-analytics-disclosure" id={disclosureId}>
        <div className="research-analytics-coverage" aria-label="统计覆盖范围">
          <span><small>共同交易日</small><strong>{view.riskAvailable ? view.riskSampleCount.toLocaleString("zh-CN") : "未形成"}</strong></span>
          <span><small>5 日基准标的</small><strong>{view.base.rows.length}</strong></span>
          <span><small>排除记录</small><strong>{view.base.omittedCount}</strong></span>
        </div>

        {view.available ? (
          <div className="research-analytics-body">
          {view.riskAvailable ? (
            <section aria-label="等权模拟组合风险">
              <header><BarChart3 size={14} aria-hidden="true" /><span><strong>等权模拟组合风险</strong><small>仅基于共同历史窗口</small></span></header>
              <div className="risk-metric-strip">
                {view.riskMetrics.map((metric) => (
                  <span key={JSON.stringify(["risk-metric", metric.key])}><small>{metric.label}</small><strong>{metric.value}</strong></span>
                ))}
              </div>
            </section>
          ) : (
            <p className="analytics-partial-note"><AlertTriangle size={13} aria-hidden="true" />共同交易日不足，未形成组合风险指标。</p>
          )}

          {view.baseAvailable ? (
            <section aria-label="五个交易日历史基准率">
              <header><BarChart3 size={14} aria-hidden="true" /><span><strong>5 日历史基准率</strong><small>非重叠窗口 · {view.base.thresholdLabel}</small></span></header>
              <div className="base-rate-table-wrap">
                <table className="base-rate-table">
                  <caption>5 个交易日非重叠历史窗口，{view.base.thresholdLabel}</caption>
                  <thead><tr><th scope="col">标的</th><th scope="col">样本</th><th scope="col">上涨基准率</th><th scope="col">下跌基准率</th></tr></thead>
                  <tbody>
                    {view.base.rows.map((row) => (
                      <tr key={JSON.stringify(["five-day-base-rate", row.symbol])}>
                        <th scope="row">{row.symbol}</th>
                        <td data-label="样本">{row.sampleCount.toLocaleString("zh-CN")}</td>
                        <td data-label="上涨基准率">{row.upRate}</td>
                        <td data-label="下跌基准率">{row.downRate}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          ) : (
            <p className="analytics-partial-note"><AlertTriangle size={13} aria-hidden="true" />没有唯一且具有正样本数的 5 日基准记录。</p>
          )}

          {view.base.omittedCount > 0 ? (
            <p className="analytics-omitted-note"><AlertTriangle size={13} aria-hidden="true" />{view.base.omittedCount} 条记录因期限、样本、标的或唯一性不满足要求而未进入摘要。</p>
          ) : null}

          <p className="analytics-caveat"><ShieldCheck size={14} aria-hidden="true" /><span>固定阈值基准率不等于策略胜率；风险指标来自等权历史模拟，不使用真实账户或仓位，也不构成预测或执行授权。</span></p>
          </div>
        ) : (
          <div className="analytics-empty" role="status"><AlertTriangle size={15} aria-hidden="true" /><span>复权历史不足，未计算或补造历史基准率与组合风险。</span></div>
        )}
      </div>
    </details>
  );
}
