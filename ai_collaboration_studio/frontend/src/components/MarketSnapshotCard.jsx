import { AlertTriangle, Clock3, RefreshCw, ShieldCheck } from "lucide-react";
import { IndustryProxySummary } from "./IndustryProxySummary";
import { OfficialEvidenceList } from "./OfficialEvidenceList";
import { ResearchAnalyticsSummary } from "./ResearchAnalyticsSummary";
import { StorageDataReadinessPanel } from "./StorageDataReadinessPanel";
import { mergePreparedResearchEvidence } from "../storageReadiness";
import { quoteFreshnessLabel } from "../marketGate";

const stateLabels = {
  ready: "数据就绪",
  degraded: "部分降级",
  offline: "当前离线",
};

function formatNumber(value, digits = 2) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return number.toLocaleString("zh-CN", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

function formatMarketTime(value) {
  if (!value) return "时间未知";
  return value.replace(/^\d{4}-\d{2}-\d{2}\s*/, "");
}

function formatPercent(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return `${number > 0 ? "+" : ""}${formatNumber(number, 1)}%`;
}

function formatCompact(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return new Intl.NumberFormat("zh-CN", { notation: "compact", maximumFractionDigits: 1 }).format(number);
}

function directionalTone(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "";
  return number >= 0 ? "up" : "down";
}

function Metric({ label, value, tone = "" }) {
  return <span className={`evidence-metric ${tone}`}><small>{label}</small><strong>{value}</strong></span>;
}

export function MarketSnapshotCard({
  roomId,
  snapshot,
  status,
  readiness,
  loading,
  readinessLoading,
  gate,
  onRefresh,
  onRefreshReadiness,
  onFreezeOfficialEvidence,
  onAddOfficialSupplement,
}) {
  const state = snapshot?.state || "offline";
  const rows = snapshot?.rows || [];
  const firstError = snapshot?.source_errors?.[0]?.message;
  const evidence = mergePreparedResearchEvidence(
    snapshot?.evidence,
    readiness?.independent_evidence?.evidence,
  );
  const technicalBySymbol = Object.fromEntries((evidence.technical?.rows || []).map((row) => [row.symbol, row]));
  const flowBySymbol = Object.fromEntries((evidence.capital_flow?.rows || []).map((row) => [row.symbol, row]));
  return (
    <div className="market-resource">
      <div className="market-resource-head">
        <span className={`market-state ${state}`}>
          {state === "ready" ? <ShieldCheck size={13} /> : <AlertTriangle size={13} />}
          {loading ? "正在读取" : stateLabels[state] || "状态未知"}
        </span>
        <button className="market-refresh" onClick={onRefresh} disabled={loading} title="重新读取富途快照">
          <RefreshCw size={13} />刷新
        </button>
      </div>

      <StorageDataReadinessPanel
        status={status}
        readiness={readiness}
        loading={readinessLoading}
        gate={gate}
        onRefresh={onRefreshReadiness}
        onAddOfficialSupplement={onAddOfficialSupplement}
      />

      {rows.length > 0 ? (
        <div className="quote-list">
          {rows.map((row) => {
            const change = Number(row.change_rate);
            const changeClass = Number.isFinite(change) ? (change >= 0 ? "positive" : "negative") : "flat";
            return (
              <div className="quote-row" key={row.symbol}>
                <span className="quote-symbol"><strong>{row.symbol?.replace("US.", "")}</strong><small title={row.freshness_basis || ""}>{quoteFreshnessLabel(row)}</small></span>
                <span className="quote-value"><strong>{formatNumber(row.last)}</strong><small className={changeClass}>{Number.isFinite(change) ? `${change >= 0 ? "+" : ""}${formatNumber(change)}%` : "—"}</small></span>
                <span className="quote-time"><Clock3 size={11} />{formatMarketTime(row.market_time)}{row.market_state ? ` · ${row.market_state}` : ""}</span>
              </div>
            );
          })}
        </div>
      ) : (
        <div className="market-offline-copy">{loading ? "正在连接本机 Futu OpenD…" : firstError || "尚未取得行情数据，不会生成替代价格。"}</div>
      )}

      {gate?.required && !gate.ready && !loading ? (
        <div className={gate.severity === "critical" ? "market-gate-reason critical" : "market-gate-reason"}>{gate.reason}</div>
      ) : null}

      {rows.length > 0 ? (
        <details className="market-evidence">
          <summary>
            <span>确定性证据</span>
            <small>{evidence.state === "ready" ? "四项齐备" : "部分数据受限"}</small>
          </summary>
          <div className="evidence-company-list">
            {rows.map((row) => {
              const technical = technicalBySymbol[row.symbol] || {};
              const flow = flowBySymbol[row.symbol] || {};
              return (
                <div className="evidence-company" key={row.symbol}>
                  <div className="evidence-company-head">
                    <strong>{row.symbol?.replace("US.", "")}</strong>
                    <small>{technical.sample_count ? `${technical.sample_count} 根复权日线` : "历史数据不足"}</small>
                  </div>
                  <div className="evidence-metric-grid">
                    <Metric label="20日" value={formatPercent(technical.return_20d_pct)} tone={directionalTone(technical.return_20d_pct)} />
                    <Metric label="RSI14" value={formatNumber(technical.rsi14, 1)} />
                    <Metric label="年化波动" value={formatPercent(technical.realized_volatility_20d_annualized_pct)} />
                    <Metric label="PE(TTM)" value={formatNumber(row.pe_ttm_ratio, 1)} />
                    <Metric label="PB" value={formatNumber(row.pb_ratio, 1)} />
                    <Metric label="5日净流" value={formatCompact(flow.net_inflow_5d)} tone={directionalTone(flow.net_inflow_5d)} />
                  </div>
                  <small className="evidence-asof">截止 {technical.as_of || row.market_time || "未知"} · 只读、向后计算</small>
                </div>
              );
            })}
          </div>
          <p>资金流是成交结构代理，不等同于新闻或社交情绪；缺少来源材料时不推断舆情。</p>
        </details>
      ) : null}

      <IndustryProxySummary evidence={evidence} />
      <ResearchAnalyticsSummary evidence={evidence} />
      <OfficialEvidenceList roomId={roomId} evidence={evidence} onFreeze={onFreezeOfficialEvidence} />

      <div className="market-resource-foot">
        <span>Futu OpenD · 只读</span>
        <span>{snapshot?.snapshot_id ? `截面 ${snapshot.snapshot_id.slice(-6)}` : "无执行能力"}</span>
      </div>
      {snapshot?.missing_symbols?.length > 0 ? <div className="missing-symbols">缺失：{snapshot.missing_symbols.join("、")}</div> : null}
    </div>
  );
}
