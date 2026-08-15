import { Activity, ExternalLink } from "lucide-react";

function formatValue(value, units) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  const digits = units === "百分比" || units === "倍" ? 2 : number >= 100 ? 0 : 2;
  const text = number.toLocaleString("zh-CN", { maximumFractionDigits: digits });
  return units === "百分比" ? `${text}%` : units === "倍" ? `${text}x` : text;
}

function formatChange(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "同比不足";
  return `12期 ${number > 0 ? "+" : ""}${number.toFixed(1)}%`;
}

export function IndustryProxySummary({ evidence }) {
  const proxy = evidence?.industry_supply_demand;
  if (!proxy?.source) return null;
  const rows = [...(proxy.derived || []), ...(proxy.rows || [])];

  return (
    <details className="industry-proxy-summary">
      <summary>
        <span><Activity size={12} />行业供需代理</span>
        <small className={proxy.state || "offline"}>{proxy.state === "ready" ? "官方月度就绪" : proxy.state === "degraded" ? "部分降级" : "当前离线"}</small>
      </summary>
      {rows.length > 0 ? (
        <div className="industry-proxy-list">
          {rows.map((row) => (
            <div className="industry-proxy-row" key={row.metric_id || row.series_id}>
              <div>
                <span className={row.scope === "device" ? "device" : "semiconductor"}>
                  {row.scope === "device" ? "设备口径" : "广义半导体"}
                </span>
                <strong>{row.label}</strong>
              </div>
              <div className="industry-proxy-value">
                <strong>{formatValue(row.latest, row.units)}</strong>
                <small>{formatChange(row.change_12_observations_pct)} · {row.as_of || "时间未知"}</small>
              </div>
              {row.source_url ? (
                <a href={row.source_url} target="_blank" rel="noreferrer" title="打开 FRED 官方序列"><ExternalLink size={11} /></a>
              ) : <span className="proxy-derived">复算</span>}
            </div>
          ))}
        </div>
      ) : <div className="industry-proxy-empty">{proxy.source_errors?.[0]?.message || "官方行业序列暂不可用，不生成替代值。"}</div>}
      <p>美国官方月度代理，不是 DRAM/NAND/HDD 即时报价。设备与广义半导体口径分开，库存/出货比也不能单独判定去库或补库。</p>
    </details>
  );
}
