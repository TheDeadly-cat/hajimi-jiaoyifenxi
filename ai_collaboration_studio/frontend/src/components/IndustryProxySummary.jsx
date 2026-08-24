import { Activity, AlertTriangle, ExternalLink, Info } from "lucide-react";
import { useMemo } from "react";
import "../styles/industry-proxy-summary.css";

const TEXT_LIMIT = 320;

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

function formatValue(value, units) {
  const number = finiteNumber(value);
  if (number === null) return "未记录";
  const digits = units === "百分比" || units === "倍" || Math.abs(number) < 100 ? 2 : 0;
  const text = number.toLocaleString("zh-CN", { maximumFractionDigits: digits });
  if (units === "百分比") return text + "%";
  if (units === "倍") return text + "x";
  return text;
}

function displayUnit(units) {
  const label = boundedText(units, 40);
  return label === "百分比" || label === "倍" ? "" : label;
}

function formatChange(value) {
  const number = finiteNumber(value);
  if (number === null) return "12 期变化不足";
  return "12 期 " + (number > 0 ? "+" : "") + number.toFixed(1) + "%";
}

function safeExternalUrl(value) {
  const candidate = String(value || "").trim();
  if (!candidate) return "";
  try {
    const parsed = new URL(candidate);
    return parsed.protocol === "http:" || parsed.protocol === "https:" ? parsed.href : "";
  } catch {
    return "";
  }
}

function normalizedState(value) {
  return value === "ready" || value === "degraded" ? value : "offline";
}

function stateLabel(state) {
  if (state === "ready") return "官方月度就绪";
  if (state === "degraded") return "部分来源降级";
  return "当前离线";
}

function scopeMeta(scope) {
  if (scope === "device") return { tone: "device", label: "设备口径" };
  if (scope === "semiconductor") return { tone: "semiconductor", label: "广义半导体" };
  return { tone: "unknown", label: "口径未记录" };
}

function dateMeta(value) {
  const raw = boundedText(value, 40);
  if (!raw) return { label: "时间未知", dateTime: "" };
  const timestamp = Date.parse(raw);
  if (!Number.isFinite(timestamp)) return { label: raw, dateTime: "" };
  const iso = new Date(timestamp).toISOString();
  return { label: iso.slice(0, 10), dateTime: iso };
}

function normalizedRows(proxy) {
  const candidates = [
    ...arrayValue(proxy?.derived).map((row) => ({ row, origin: "derived" })),
    ...arrayValue(proxy?.rows).map((row) => ({ row, origin: "source" })),
  ];
  const uniqueRows = new Map();

  for (const candidate of candidates) {
    if (!candidate.row || typeof candidate.row !== "object") continue;
    const metricId = boundedText(candidate.row.metric_id, 120);
    const seriesId = boundedText(candidate.row.series_id, 120);
    const label = boundedText(candidate.row.label) || "未命名代理指标";
    const scope = scopeMeta(candidate.row.scope);
    const identity = JSON.stringify([
      "industry-proxy-row",
      candidate.origin,
      metricId,
      seriesId,
      scope.tone,
      label,
    ]);
    if (uniqueRows.has(identity)) continue;
    uniqueRows.set(identity, {
      key: identity,
      origin: candidate.origin,
      metricId,
      seriesId,
      label,
      scope,
      latest: candidate.row.latest,
      units: boundedText(candidate.row.units, 40),
      change: candidate.row.change_12_observations_pct,
      asOf: dateMeta(candidate.row.as_of),
      sourceUrl: safeExternalUrl(candidate.row.source_url),
    });
  }

  return Array.from(uniqueRows.values());
}

function sourceErrorMessage(proxy) {
  const firstError = arrayValue(proxy?.source_errors)[0];
  const message = typeof firstError?.message === "string" ? firstError.message : "";
  return boundedText(message, 1000) || "官方行业序列暂不可用，不生成替代值。";
}

export function IndustryProxySummary({ evidence }) {
  const proxy = evidence?.industry_supply_demand;
  const rows = useMemo(() => normalizedRows(proxy), [proxy]);

  if (!proxy?.source) return null;

  const state = normalizedState(proxy.state);
  const derivedCount = rows.filter((row) => row.origin === "derived").length;
  const sourceLabel = typeof proxy.source === "string"
    ? boundedText(proxy.source, 100)
    : "来源已记录";

  return (
    <details className="industry-proxy-summary industry-proxy-summary-v2">
      <summary>
        <span><Activity size={15} aria-hidden="true" />行业供需代理</span>
        <small className={state}>{stateLabel(state)}</small>
      </summary>

      <div className="industry-proxy-overview" aria-label="行业代理摘要">
        <span><small>可见指标</small><strong>{rows.length}</strong></span>
        <span><small>本地复算</small><strong>{derivedCount}</strong></span>
        <span className="source"><small>来源标识</small><strong>{sourceLabel}</strong></span>
      </div>

      {rows.length > 0 ? (
        <div className="industry-proxy-list" role="list" aria-label="行业供需代理指标">
          {rows.map((row) => {
            const unit = displayUnit(row.units);
            return (
              <article className={"industry-proxy-row " + row.origin} key={row.key} role="listitem">
                <header>
                  <span className={row.scope.tone}>{row.scope.label}</span>
                  <em className={row.origin}>{row.origin === "derived" ? "本地复算" : "官方序列"}</em>
                </header>
                <h4>{row.label}</h4>
                <div className="industry-proxy-value">
                  <span><strong>{formatValue(row.latest, row.units)}</strong>{unit ? <em>{unit}</em> : null}</span>
                  <small>{formatChange(row.change)}</small>
                </div>
                <footer>
                  <time dateTime={row.asOf.dateTime || undefined}>截至 {row.asOf.label}</time>
                  {row.sourceUrl ? (
                    <a href={row.sourceUrl} target="_blank" rel="noopener noreferrer" aria-label={(row.origin === "derived" ? "打开基础官方序列" : "打开官方序列") + "，新窗口打开"}>
                      <ExternalLink size={12} aria-hidden="true" />{row.origin === "derived" ? "基础序列" : "官方序列"}
                    </a>
                  ) : (
                    <span className="proxy-source-missing">{row.origin === "derived" ? "复算来源链接缺失" : "官方链接缺失"}</span>
                  )}
                </footer>
              </article>
            );
          })}
        </div>
      ) : (
        <div className="industry-proxy-empty" role="status">
          <AlertTriangle size={15} aria-hidden="true" />
          <span>{sourceErrorMessage(proxy)}</span>
        </div>
      )}

      <p className="industry-proxy-boundary"><Info size={14} aria-hidden="true" /><span>美国官方月度代理，不是 DRAM、NAND 或 HDD 即时报价。设备与广义半导体口径分开，库存或出货比不能单独判定去库或补库。</span></p>
    </details>
  );
}
