import { Activity, AlertTriangle, ShieldCheck } from "lucide-react";


function percent(value, digits = 1) {
  const number = Number(value);
  return Number.isFinite(number) ? `${number.toFixed(digits)}%` : "—";
}


export function ResearchAnalyticsSummary({ evidence }) {
  const analytics = evidence?.research_analytics;
  if (!analytics) return null;
  const baseRates = analytics.base_rates || {};
  const risk = analytics.portfolio_risk || {};
  const fiveDayRows = (baseRates.rows || []).filter((row) => row.horizon_days === 5);
  const available = fiveDayRows.some((row) => row.sample_count > 0) || risk.sample_count > 0;
  return (
    <details className="research-analytics" open={available}>
      <summary>
        <span><Activity size={13} />历史基准与等权模拟风险</span>
        <small>{available ? `${risk.sample_count || 0} 个共同交易日` : "历史数据不足"}</small>
      </summary>
      {available ? <>
        <div className="risk-metric-strip" aria-label="等权模拟组合风险指标">
          <span><small>年化波动</small><strong>{percent(risk.annualized_volatility_pct)}</strong></span>
          <span><small>最大回撤</small><strong>{percent(risk.max_drawdown_pct)}</strong></span>
          <span><small>单日 VaR 95%</small><strong>{percent(risk.historical_var_95_1d_pct)}</strong></span>
          <span><small>最差 5 日</small><strong>{percent(risk.worst_5d_pct)}</strong></span>
        </div>
        <div className="base-rate-table-wrap">
          <table className="base-rate-table">
            <caption>5 个交易日、±{baseRates.threshold_pct ?? 2}% 阈值的非重叠历史窗口</caption>
            <thead><tr><th scope="col">标的</th><th scope="col">样本</th><th scope="col">上涨基准率</th><th scope="col">下跌基准率</th></tr></thead>
            <tbody>{fiveDayRows.map((row) => <tr key={row.symbol}>
              <th scope="row">{row.symbol?.replace("US.", "")}</th>
              <td>{row.sample_count}</td>
              <td>{percent(row.up_base_rate_pct)}</td>
              <td>{percent(row.down_base_rate_pct)}</td>
            </tr>)}</tbody>
          </table>
        </div>
        <p className="analytics-caveat"><ShieldCheck size={13} />固定阈值基准率不等于策略胜率；风险指标来自等权历史模拟，不使用真实账户或仓位。</p>
      </> : <div className="analytics-empty"><AlertTriangle size={14} />富途复权历史不足，未计算或补造历史胜率与组合风险。</div>}
    </details>
  );
}
