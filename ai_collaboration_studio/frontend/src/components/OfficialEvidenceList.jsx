import { AlertTriangle, Check, ExternalLink, FileCheck2, LoaderCircle, ShieldCheck } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

const EVENT_LIMIT = 16;
const TEXT_LIMIT = 320;
const ERROR_LIMIT = 1000;

function arrayValue(value) {
  return Array.isArray(value) ? value : [];
}

function boundedText(value, limit = TEXT_LIMIT) {
  const text = String(value ?? "").trim();
  return text.length <= limit ? text : text.slice(0, limit) + "...";
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

function shortSymbol(symbol) {
  return boundedText(symbol, 40).replace(/^US\./, "") || "标的未知";
}

function eventIdentity(event) {
  return JSON.stringify([
    "official-evidence",
    event.evidence_kind,
    event.symbol,
    event.official_url,
  ]);
}

function eventKey(roomId, event) {
  return JSON.stringify([
    "official-evidence-room",
    String(roomId || ""),
    event.evidence_kind,
    event.symbol,
    event.official_url,
  ]);
}

function eventTimestamp(value) {
  const raw = String(value ?? "").trim();
  if (!raw) return null;
  if (/^-?\d+(\.\d+)?$/.test(raw)) {
    const numeric = Number(raw);
    if (Number.isFinite(numeric)) {
      const milliseconds = Math.abs(numeric) < 1e12 ? numeric * 1000 : numeric;
      const numericDate = new Date(milliseconds);
      if (!Number.isNaN(numericDate.getTime())) return numericDate.getTime();
    }
  }
  const parsed = Date.parse(raw);
  return Number.isFinite(parsed) ? parsed : null;
}

function eventDateMeta(value) {
  const timestamp = eventTimestamp(value);
  if (timestamp !== null) {
    const iso = new Date(timestamp).toISOString();
    return { label: iso.slice(0, 10), dateTime: iso };
  }
  const raw = boundedText(value, 24);
  return { label: raw || "时间未知", dateTime: "" };
}

function normalizedMetrics(value) {
  return arrayValue(value)
    .filter((metric) => metric && typeof metric === "object")
    .slice(0, 3)
    .map((metric) => ({
      metric_id: boundedText(metric.metric_id, 120),
      metric_name: boundedText(metric.metric_name, 120) || "指标未命名",
      source_locator: boundedText(metric.source_locator, 180),
      value_text: boundedText(metric.value_text, 240) || "未记录",
      fact_or_guidance: boundedText(metric.fact_or_guidance, 40),
    }));
}

function officialMaterial(pack) {
  const options = [
    [pack?.presentation_url, "官方演示"],
    [pack?.prepared_remarks_url, "管理层讲稿"],
    [pack?.supplemental_url, "补充财务资料"],
    [pack?.presentation_hub_url, "材料入口"],
  ];
  for (const [candidate, label] of options) {
    const url = safeExternalUrl(candidate);
    if (url) return { url, label };
  }
  return { url: "", label: "" };
}

function collectEvents(evidence) {
  const events = [];
  const earningsReleaseKeys = new Set();

  for (const row of arrayValue(evidence?.official_earnings_packs?.rows)) {
    const symbol = boundedText(row?.symbol, 40);
    for (const pack of arrayValue(row?.packs)) {
      const officialUrl = safeExternalUrl(pack?.release_url);
      if (!officialUrl) continue;
      const metrics = arrayValue(pack?.metrics);
      const secMatches = arrayValue(pack?.possible_sec_matches);
      const material = officialMaterial(pack);
      earningsReleaseKeys.add(JSON.stringify([symbol, officialUrl]));
      events.push({
        evidence_kind: "ir_release",
        symbol,
        official_url: officialUrl,
        title: boundedText(pack?.title) || "官方季度业绩材料包",
        date: pack?.published_at || pack?.published_date || "",
        source: "季度业绩包",
        sourceClass: "earnings",
        note: boundedText((boundedText(pack?.fiscal_period, 80) || "期间待核验") + " · " + metrics.length + " 项公司自述指标 · " + secMatches.length + " 条 SEC 候选"),
        metrics: normalizedMetrics(metrics),
        material_url: material.url,
        material_label: material.label,
      });
    }
  }

  for (const row of arrayValue(evidence?.company_ir_releases?.rows)) {
    const symbol = boundedText(row?.symbol, 40);
    for (const release of arrayValue(row?.releases)) {
      const officialUrl = safeExternalUrl(release?.official_url);
      if (!officialUrl) continue;
      if (earningsReleaseKeys.has(JSON.stringify([symbol, officialUrl]))) continue;
      events.push({
        evidence_kind: "ir_release",
        symbol,
        official_url: officialUrl,
        title: boundedText(release?.title) || "公司官方新闻稿",
        date: release?.published_at || release?.published_date || "",
        source: "公司 IR",
        sourceClass: "ir",
        note: boundedText(arrayValue(release?.possible_sec_matches).length + " 条 SEC 日期关联候选"),
        metrics: [],
        material_url: "",
        material_label: "",
      });
    }
  }

  for (const row of arrayValue(evidence?.official_filings?.rows)) {
    const symbol = boundedText(row?.symbol, 40);
    for (const filing of arrayValue(row?.filings)) {
      const officialUrl = safeExternalUrl(filing?.official_url);
      if (!officialUrl) continue;
      events.push({
        evidence_kind: "sec_filing",
        symbol,
        official_url: officialUrl,
        title: boundedText((boundedText(filing?.form, 40) || "SEC") + " · " + (boundedText(filing?.description, 240) || "官方申报")),
        date: filing?.accepted_at || filing?.filing_date || "",
        source: "SEC EDGAR",
        sourceClass: "sec",
        note: filing?.report_date ? "报告期 " + boundedText(filing.report_date, 40) : "监管一手索引",
        metrics: [],
        material_url: "",
        material_label: "",
      });
    }
  }

  const uniqueEvents = new Map();
  for (const event of events) {
    const identity = eventIdentity(event);
    if (!uniqueEvents.has(identity)) uniqueEvents.set(identity, event);
  }

  return Array.from(uniqueEvents.values())
    .sort((left, right) => {
      const leftTimestamp = eventTimestamp(left.date) ?? Number.NEGATIVE_INFINITY;
      const rightTimestamp = eventTimestamp(right.date) ?? Number.NEGATIVE_INFINITY;
      if (leftTimestamp !== rightTimestamp) return rightTimestamp > leftTimestamp ? 1 : -1;
      return eventIdentity(left).localeCompare(eventIdentity(right));
    })
    .slice(0, EVENT_LIMIT);
}

function freezeErrorMessage(error) {
  const message = typeof error?.message === "string" ? error.message.trim() : "";
  return boundedText(message || "冻结官方证据失败，请重试。", ERROR_LIMIT);
}

export function OfficialEvidenceList({ roomId, evidence, onFreeze }) {
  const events = useMemo(() => collectEvents(evidence), [evidence]);
  const [freezingKeys, setFreezingKeys] = useState(() => new Set());
  const [resultByKey, setResultByKey] = useState({});
  const [errorByKey, setErrorByKey] = useState({});
  const freezingKeysRef = useRef(new Set());
  const mountedRef = useRef(false);
  const roomGenerationRef = useRef(0);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      freezingKeysRef.current.clear();
    };
  }, []);

  useEffect(() => {
    roomGenerationRef.current += 1;
    freezingKeysRef.current.clear();
    setFreezingKeys(new Set());
    setResultByKey({});
    setErrorByKey({});
  }, [roomId]);

  if (events.length === 0) return null;

  const freeze = async (event) => {
    const key = eventKey(roomId, event);
    if (typeof onFreeze !== "function" || freezingKeysRef.current.has(key) || resultByKey[key]) return;

    const generation = roomGenerationRef.current;
    freezingKeysRef.current.add(key);
    setFreezingKeys(new Set(freezingKeysRef.current));
    setErrorByKey((current) => {
      if (!current[key]) return current;
      const next = { ...current };
      delete next[key];
      return next;
    });

    try {
      const result = await onFreeze({
        evidence_kind: event.evidence_kind,
        symbol: event.symbol,
        official_url: event.official_url,
      });
      if (!result || typeof result.created !== "boolean") {
        throw new Error("冻结响应缺少明确 created 状态。");
      }
      if (!mountedRef.current || generation !== roomGenerationRef.current) return;
      setResultByKey((current) => ({
        ...current,
        [key]: result.created ? "已冻结" : "已存在",
      }));
    } catch (requestError) {
      if (!mountedRef.current || generation !== roomGenerationRef.current) return;
      setErrorByKey((current) => ({ ...current, [key]: freezeErrorMessage(requestError) }));
    } finally {
      freezingKeysRef.current.delete(key);
      if (mountedRef.current && generation === roomGenerationRef.current) {
        setFreezingKeys(new Set(freezingKeysRef.current));
      }
    }
  };

  const freezeAvailable = typeof onFreeze === "function";

  return (
    <details className="official-evidence-list">
      <summary>
        <span><ShieldCheck size={14} aria-hidden="true" />官方事件证据</span>
        <small>{events.length} 条可核验来源</small>
      </summary>
      <div className="official-event-list" role="list" aria-label="官方事件证据">
        {events.map((event) => {
          const key = eventKey(roomId, event);
          const result = resultByKey[key];
          const freezeError = errorByKey[key];
          const busy = freezingKeys.has(key);
          const dateMeta = eventDateMeta(event.date);
          const materialUrl = event.material_url !== event.official_url ? event.material_url : "";
          return (
            <article className="official-event" key={key} role="listitem" aria-busy={busy}>
              <div className="official-event-heading">
                <span className={event.sourceClass}>{event.source}</span>
                <strong>{shortSymbol(event.symbol)}</strong>
                <time dateTime={dateMeta.dateTime || undefined}>{dateMeta.label}</time>
              </div>
              <h4 className="official-event-title">{event.title}</h4>
              {event.metrics.length ? (
                <div className="official-metric-list" aria-label="公司披露指标">
                  {event.metrics.map((metric, metricIndex) => {
                    const metricTone = metric.fact_or_guidance === "company_guidance"
                      ? "guidance"
                      : metric.fact_or_guidance === "historical_fact"
                        ? "fact"
                        : "unknown";
                    const metricLabel = metricTone === "guidance"
                      ? "公司指引"
                      : metricTone === "fact"
                        ? "历史事实"
                        : "口径未记录";
                    return (
                      <span key={JSON.stringify(["official-metric", metric.metric_id, metric.metric_name, metric.source_locator, metricIndex])}>
                        <strong>{metric.metric_name}</strong>
                        <b>{metric.value_text}</b>
                        <em className={metricTone}>{metricLabel}</em>
                      </span>
                    );
                  })}
                </div>
              ) : null}
              <div className="official-event-actions">
                <small>{event.note}</small>
                <span className="official-event-links">
                  <a className="primary" href={event.official_url} target="_blank" rel="noopener noreferrer" aria-label={event.source + " 原始来源，新窗口打开"}>
                    <ExternalLink size={11} aria-hidden="true" />原始来源
                  </a>
                  {materialUrl ? (
                    <a className="material" href={materialUrl} target="_blank" rel="noopener noreferrer" aria-label={(event.material_label || "材料入口") + "，新窗口打开"}>
                      <ExternalLink size={11} aria-hidden="true" />{event.material_label || "材料入口"}
                    </a>
                  ) : null}
                </span>
                <button
                  type="button"
                  className={result ? "frozen" : ""}
                  onClick={() => freeze(event)}
                  disabled={busy || Boolean(result) || !freezeAvailable}
                  title={!freezeAvailable ? "当前视图未提供资料冻结能力" : undefined}
                >
                  {busy ? <LoaderCircle className="spin" size={12} aria-hidden="true" /> : result ? <Check size={12} aria-hidden="true" /> : <FileCheck2 size={12} aria-hidden="true" />}
                  {busy ? "冻结中" : result || (freezeAvailable ? "冻结为资料" : "冻结不可用")}
                </button>
              </div>
              {freezeError ? <p className="official-event-error" role="alert"><AlertTriangle size={13} aria-hidden="true" /><span>{freezeError}</span></p> : null}
            </article>
          );
        })}
      </div>
      <p className="official-evidence-boundary">季度业绩包只整理期间、官方入口和口径断点；SEC 是监管申报索引，公司 IR 是一手自述。冻结后才获得房间资料 ID，关联候选仍需打开原文核验。</p>
    </details>
  );
}
