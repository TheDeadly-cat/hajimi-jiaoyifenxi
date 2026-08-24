import { AlertTriangle, Clock3, RefreshCw, ShieldCheck } from "lucide-react";
import { memo, useMemo } from "react";
import { IndustryProxySummary } from "./IndustryProxySummary";
import { OfficialEvidenceList } from "./OfficialEvidenceList";
import { ResearchAnalyticsSummary } from "./ResearchAnalyticsSummary";
import { StorageDataReadinessPanel } from "./StorageDataReadinessPanel";
import { mergePreparedResearchEvidence } from "../storageReadiness";
import { quoteFreshnessLabel } from "../marketGate";
import "../styles/market-snapshot-polish.css";

const EMPTY_LIST = Object.freeze([]);
const stateLabels = Object.freeze({
  ready: "截面已载入",
  degraded: "截面部分降级",
  offline: "截面不可用",
});
const FIXED_NUMBER_FORMATTERS = Object.freeze({
  1: new Intl.NumberFormat("zh-CN", { minimumFractionDigits: 1, maximumFractionDigits: 1 }),
  2: new Intl.NumberFormat("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 }),
});
const COMPACT_NUMBER_FORMATTER = new Intl.NumberFormat("zh-CN", {
  notation: "compact",
  maximumFractionDigits: 1,
});

function formatNumber(value, digits = 2) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return (FIXED_NUMBER_FORMATTERS[digits] || FIXED_NUMBER_FORMATTERS[2]).format(number);
}

function formatMarketTime(value) {
  const text = typeof value === "string" ? value.trim() : "";
  return text ? text.replace(/^\d{4}-\d{2}-\d{2}\s*/, "") : "时间未知";
}

function formatSymbol(value) {
  const symbol = typeof value === "string" ? value.trim() : "";
  return symbol ? symbol.replace(/^US\./, "") : "未知标的";
}

function formatPercent(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return `${number > 0 ? "+" : ""}${formatNumber(number, 1)}%`;
}

function formatCompact(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return COMPACT_NUMBER_FORMATTER.format(number);
}

function directionalTone(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "";
  return number >= 0 ? "up" : "down";
}

const Metric = memo(function Metric({ label, value, tone = "" }) {
  return <span className={`evidence-metric ${tone}`}><small>{label}</small><strong>{value}</strong></span>;
});

export const MarketSnapshotCard = memo(function MarketSnapshotCard({
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
  const rawState = typeof snapshot?.state === "string" ? snapshot.state : "offline";
  const state = stateLabels[rawState] ? rawState : "unknown";
  const rows = Array.isArray(snapshot?.rows) ? snapshot.rows : EMPTY_LIST;
  const sourceErrors = Array.isArray(snapshot?.source_errors) ? snapshot.source_errors : EMPTY_LIST;
  const firstError = typeof sourceErrors[0]?.message === "string" ? sourceErrors[0].message : "";
  const snapshotEvidence = snapshot?.evidence;
  const readinessEvidence = readiness?.independent_evidence?.evidence;
  const evidence = useMemo(
    () => mergePreparedResearchEvidence(snapshotEvidence, readinessEvidence),
    [readinessEvidence, snapshotEvidence],
  );
  const technicalRows = Array.isArray(evidence.technical?.rows) ? evidence.technical.rows : EMPTY_LIST;
  const flowRows = Array.isArray(evidence.capital_flow?.rows) ? evidence.capital_flow.rows : EMPTY_LIST;
  const { technicalBySymbol, flowBySymbol } = useMemo(() => {
    const technicalIndex = new Map();
    const flowIndex = new Map();
    for (const row of technicalRows) {
      if (typeof row?.symbol === "string" && row.symbol) technicalIndex.set(row.symbol, row);
    }
    for (const row of flowRows) {
      if (typeof row?.symbol === "string" && row.symbol) flowIndex.set(row.symbol, row);
    }
    return { technicalBySymbol: technicalIndex, flowBySymbol: flowIndex };
  }, [flowRows, technicalRows]);
  const missingSymbols = Array.isArray(snapshot?.missing_symbols) ? snapshot.missing_symbols : EMPTY_LIST;
  const snapshotId = typeof snapshot?.snapshot_id === "string" ? snapshot.snapshot_id.trim() : "";
  return (
    <div className="market-resource market-resource-workbench" aria-busy={Boolean(loading || readinessLoading)}>
      <div className="market-resource-head">
        <span className={`market-state ${state}`} role="status" aria-live="polite">
          {state === "ready" ? <ShieldCheck aria-hidden="true" size={13} /> : <AlertTriangle aria-hidden="true" size={13} />}
          {loading ? "正在读取" : stateLabels[state] || "状态未知"}
        </span>
        <button className="market-refresh" type="button" onClick={onRefresh} disabled={loading || typeof onRefresh !== "function"} aria-busy={loading} title="重新读取富途快照">
          <RefreshCw aria-hidden="true" size={13} />刷新
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
        <div className="quote-list" role="list" aria-label="只读行情截面">
          {rows.map((row) => {
            const change = Number(row.change_rate);
            const changeClass = Number.isFinite(change) ? (change >= 0 ? "positive" : "negative") : "flat";
            return (
              <div className="quote-row" key={row.symbol} role="listitem">
                <span className="quote-symbol"><strong>{formatSymbol(row.symbol)}</strong><small title={row.freshness_basis || ""}>{quoteFreshnessLabel(row)}</small></span>
                <span className="quote-value"><strong>{formatNumber(row.last)}</strong><small className={changeClass}>{Number.isFinite(change) ? `${change >= 0 ? "+" : ""}${formatNumber(change)}%` : "—"}</small></span>
                <span className="quote-time"><Clock3 aria-hidden="true" size={11} />{formatMarketTime(row.market_time)}{row.market_state ? ` · ${row.market_state}` : ""}</span>
              </div>
            );
          })}
        </div>
      ) : (
        <div className="market-offline-copy" role={loading ? "status" : "note"}>{loading ? "正在连接本机 Futu OpenD…" : firstError || "尚未取得行情数据，不会生成替代价格。"}</div>
      )}

      {gate?.required && !gate.ready && !loading ? (
        <div className={gate.severity === "critical" ? "market-gate-reason critical" : "market-gate-reason"} role={gate.severity === "critical" ? "alert" : "note"}>{gate.reason}</div>
      ) : null}

      {rows.length > 0 ? (
        <details className="market-evidence">
          <summary>
            <span>确定性证据</span>
            <small>{evidence.state === "ready" ? "四项齐备" : "部分数据受限"}</small>
          </summary>
          <div className="evidence-company-list" role="list" aria-label="按标的确定性证据">
            {rows.map((row) => {
              const technical = technicalBySymbol.get(row.symbol) || {};
              const flow = flowBySymbol.get(row.symbol) || {};
              return (
                <div className="evidence-company" key={row.symbol} role="listitem">
                  <div className="evidence-company-head">
                    <strong>{formatSymbol(row.symbol)}</strong>
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
        <span>{snapshotId ? `截面 ${snapshotId.slice(-6)}` : "无执行能力"}</span>
      </div>
      {missingSymbols.length > 0 ? <div className="missing-symbols" role="note">缺失：{missingSymbols.map(formatSymbol).join("、")}</div> : null}
    </div>
  );
});
