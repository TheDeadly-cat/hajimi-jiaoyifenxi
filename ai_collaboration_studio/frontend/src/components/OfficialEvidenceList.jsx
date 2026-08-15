import { Check, ExternalLink, FileCheck2, LoaderCircle, ShieldCheck } from "lucide-react";
import { useMemo, useState } from "react";

function shortSymbol(symbol) {
  return String(symbol || "").replace("US.", "");
}

function eventKey(roomId, event) {
  return `${roomId}:${event.evidence_kind}:${event.symbol}:${event.official_url}`;
}

function collectEvents(evidence) {
  const events = [];
  const earningsReleaseUrls = new Set();
  for (const row of evidence?.official_earnings_packs?.rows || []) {
    for (const pack of row.packs || []) {
      if (!pack.release_url) continue;
      earningsReleaseUrls.add(pack.release_url);
      events.push({
        evidence_kind: "ir_release",
        symbol: row.symbol,
        official_url: pack.release_url,
        title: pack.title || "官方季度业绩材料包",
        date: pack.published_at || pack.published_date || "",
        source: "季度业绩包",
        sourceClass: "earnings",
        note: `${pack.fiscal_period || "期间待核验"} · ${(pack.metrics || []).length} 项公司自述指标 · ${(pack.possible_sec_matches || []).length} 条 SEC 候选`,
        metrics: pack.metrics || [],
        material_url: pack.presentation_url || pack.prepared_remarks_url || pack.supplemental_url || pack.presentation_hub_url || "",
        material_label: pack.presentation_url
          ? "官方演示"
          : pack.prepared_remarks_url
            ? "管理层讲稿"
            : pack.supplemental_url
              ? "补充财务资料"
              : "材料入口",
      });
    }
  }
  for (const row of evidence?.company_ir_releases?.rows || []) {
    for (const release of row.releases || []) {
      if (!release.official_url) continue;
      if (earningsReleaseUrls.has(release.official_url)) continue;
      events.push({
        evidence_kind: "ir_release",
        symbol: row.symbol,
        official_url: release.official_url,
        title: release.title || "公司官方新闻稿",
        date: release.published_at || release.published_date || "",
        source: "公司 IR",
        sourceClass: "ir",
        note: `${(release.possible_sec_matches || []).length} 条 SEC 日期关联候选`,
      });
    }
  }
  for (const row of evidence?.official_filings?.rows || []) {
    for (const filing of row.filings || []) {
      if (!filing.official_url) continue;
      events.push({
        evidence_kind: "sec_filing",
        symbol: row.symbol,
        official_url: filing.official_url,
        title: `${filing.form || "SEC"} · ${filing.description || "官方申报"}`,
        date: filing.accepted_at || filing.filing_date || "",
        source: "SEC EDGAR",
        sourceClass: "sec",
        note: filing.report_date ? `报告期 ${filing.report_date}` : "监管一手索引",
      });
    }
  }
  return events
    .sort((left, right) => String(right.date).localeCompare(String(left.date)))
    .slice(0, 16);
}

export function OfficialEvidenceList({ roomId, evidence, onFreeze }) {
  const events = useMemo(() => collectEvents(evidence), [evidence]);
  const [freezingKey, setFreezingKey] = useState("");
  const [resultByKey, setResultByKey] = useState({});

  if (events.length === 0) return null;

  const freeze = async (event) => {
    const key = eventKey(roomId, event);
    setFreezingKey(key);
    try {
      const result = await onFreeze?.({
        evidence_kind: event.evidence_kind,
        symbol: event.symbol,
        official_url: event.official_url,
      });
      setResultByKey((current) => ({ ...current, [key]: result?.created ? "已冻结" : "已存在" }));
    } finally {
      setFreezingKey("");
    }
  };

  return (
    <details className="official-evidence-list">
      <summary>
        <span><ShieldCheck size={12} />官方事件证据</span>
        <small>{events.length} 条可选</small>
      </summary>
      <div className="official-event-list">
        {events.map((event) => {
          const key = eventKey(roomId, event);
          const result = resultByKey[key];
          const busy = freezingKey === key;
          return (
            <article className="official-event" key={key}>
              <div className="official-event-heading">
                <span className={event.sourceClass || (event.evidence_kind === "sec_filing" ? "sec" : "ir")}>{event.source}</span>
                <strong>{shortSymbol(event.symbol)}</strong>
                <time>{event.date ? String(event.date).slice(0, 10) : "时间未知"}</time>
              </div>
              <p>{event.title}</p>
              {event.metrics?.length ? (
                <div className="official-metric-list">
                  {event.metrics.slice(0, 3).map((metric) => (
                    <span key={metric.metric_id || `${metric.metric_name}:${metric.source_locator}`}>
                      <strong>{metric.metric_name}</strong>
                      <b>{metric.value_text}</b>
                      <em className={metric.fact_or_guidance === "company_guidance" ? "guidance" : "fact"}>
                        {metric.fact_or_guidance === "company_guidance" ? "公司指引" : "历史事实"}
                      </em>
                    </span>
                  ))}
                </div>
              ) : null}
              <div className="official-event-actions">
                <small>{event.note}</small>
                {event.material_url ? (
                  <a href={event.material_url} target="_blank" rel="noreferrer">
                    <ExternalLink size={10} />{event.material_label || "材料入口"}
                  </a>
                ) : null}
                <button type="button" onClick={() => freeze(event)} disabled={busy || Boolean(result) || !onFreeze}>
                  {busy ? <LoaderCircle className="spin" size={11} /> : result ? <Check size={11} /> : <FileCheck2 size={11} />}
                  {busy ? "冻结中" : result || "冻结为资料"}
                </button>
              </div>
            </article>
          );
        })}
      </div>
      <p>季度业绩包只整理期间、官方入口和口径断点；SEC 是监管申报索引，公司 IR 是一手自述。冻结后才获得房间资料 ID，关联候选仍需打开原文核验。</p>
    </details>
  );
}
