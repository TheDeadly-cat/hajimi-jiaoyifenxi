import React, { useEffect, useRef, useState } from "react";
import { BookOpen, History, LoaderCircle } from "lucide-react";
import "./NewsEventCard.css";
import { NewsEventActions } from "./NewsEventActions";
import { AnalysisEvidence, AnalysisReview, EvidencePackageScope, RevisionComparisonScope, analysisStateLabel } from "./NewsContentQuality";
import { validatedEvidencePreview, evidenceOmissionLabel, attachmentReadingLabel, attachmentReasonLabel } from "../newsEvidence";

export const contentStateLabels = {
  headline_only: "仅有标题", excerpt_only: "已读订阅摘录", full_text: "已取得正文",
  partial_text: "正文不完整", fetch_failed: "原文读取失败", access_restricted: "原文访问受限", retracted: "来源已撤回",
};
const taskLabels = { pending: "待分析", running: "处理中", unknown: "结果未确认", completed: "已完成", needs_review: "待复核", failed: "失败", cancelled: "已取消", superseded: "已有新版本" };
const fieldLabels = { title: "标题", excerpt: "摘录", body_text: "正文", published_at: "来源发布时间", source_updated_at: "来源修订时间", content_status: "材料状态", evidence: "证据内容" };
const strings = values => Array.isArray(values) ? values.filter(v => typeof v === "string" && v.trim()) : [];
export function eventTime(value) {
  if (!value) return "未知";
  const date = new Date(typeof value === "number" ? value * 1000 : value);
  return Number.isFinite(date.getTime()) ? date.toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false }) : "未知";
}
export function eventLink(value) {
  try { const u = new URL(value); return u.protocol === "https:" && !u.username && !u.password ? u.href : ""; } catch { return ""; }
}

function EvidenceLink({ url, children }) { const href = eventLink(url); return href ? <a href={href} target="_blank" rel="noopener noreferrer">{children}</a> : <span>{children}（链接不可用）</span>; }

function EvidenceText({ evidence }) {
  const segments = Array.isArray(evidence.segments) ? evidence.segments : [...(evidence.paragraphs || []), ...(evidence.tables || [])];
  return <details className="news-event-evidence"><summary>查看已保存正文与表格 <span>{segments.length ? `${segments.length} 处定位` : "实际读取范围"}</span></summary>
    {segments.length ? <ol>{segments.map((segment, index) => <li key={`${segment.id || index}`}><strong>{segment.kind === "table" || String(segment.id).startsWith("table") ? "表格" : "段落"} {segment.id || index + 1}</strong><p>{segment.text}</p></li>)}</ol> : <p className="news-event-body">{evidence.body_text || "尚未保存正文；现有摘录仍可阅读。"}</p>}
    {evidence.truncated ? <p className="news-event-warning">保存内容有截断，未取得的部分不能作为判断依据。</p> : null}
    {evidence.extractor_version ? <details><summary>读取记录</summary><p>正文取得：{eventTime(evidence.fetched_at)}（北京时间）</p><p>抽取版本：{evidence.extractor_version}</p></details> : null}
  </details>;
}

function EvidencePreview({ preview, versionId, evidence }) {
  const checked = validatedEvidencePreview(preview, { versionId, contentHash: evidence.content_hash });
  return <details className="news-evidence-preview" open><summary>可用依据预览（未发送）</summary>
    <p>只查看本版本可选入的材料，不调用模型。已保存分析实际使用的范围另列。</p>
    {checked.valid ? <>{checked.status === "no_selected_evidence" ? <p className="news-event-warning">本版本有保存正文，但没有可完整选入的依据组。</p> : null}<EvidencePackageScope evidencePackage={checked.package} segments={checked.segments} /><RevisionComparisonScope comparison={preview?.material?.revision_comparison} /></> : <p className="news-event-warning">{evidenceOmissionLabel(checked.reason)}。保存的材料仍可在下方查看。</p>}
  </details>;
}

export function NewsEventCard({ item, apiClient, busy, onRead, marketImpact, onHistoryChange, onUpdated }) {
  const event = item.event;
  const [versions, setVersions] = useState(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [historyBusy, setHistoryBusy] = useState(false);
  const [historyError, setHistoryError] = useState("");
  const [selectedVersion, setSelectedVersion] = useState("");
  const [includeAttachments, setIncludeAttachments] = useState(false);
  const inFlight = useRef(null);
  useEffect(() => () => { inFlight.current?.abort(); }, []);
  useEffect(() => { inFlight.current?.abort(); setVersions(null); setSelectedVersion(""); setHistoryOpen(false); setHistoryError(""); setHistoryBusy(false); onHistoryChange?.(false, null); }, [item.id, item.current_version_id, onHistoryChange]);
  if (!event) return null;
  const current = event.current_version || {};
  const historical = selectedVersion && selectedVersion !== current.id;
  const version = historical ? versions?.find(v => v.id === selectedVersion) || {} : current;
  const evidence = version.evidence || (historical ? null : item.evidence) || {};
  const preview = historical ? version.evidence_preview : item.evidence_preview || version.evidence_preview;
  const state = version.content_status || evidence.content_status || evidence.status || (item.excerpt ? "excerpt_only" : "headline_only");
  const attachments = Array.isArray(evidence.attachments) ? evidence.attachments : [];
  const changes = Array.isArray(version.changes) ? version.changes : [];
  const origins = Array.isArray(event.sources) ? event.sources : [];
  const symbols = strings(historical ? version.symbols || evidence.symbols : item.symbols || evidence.symbols || event.symbols);
  const impact = marketImpact || item.market_impact;
  const loadHistory = async () => {
    setHistoryOpen(value => !value);
    if (versions || historyBusy || typeof apiClient.newsVersions !== "function") return;
    const controller = new AbortController(); inFlight.current = controller; setHistoryBusy(true); setHistoryError("");
    try {
      const result = await apiClient.newsVersions(item.id, controller.signal);
      if (controller.signal.aborted) return;
      const rows = Array.isArray(result.versions) ? result.versions : result.event?.versions;
      if (!Array.isArray(rows)) throw new Error("版本记录尚未确认，请重新读取。");
      setVersions(rows);
    } catch (error) { if (!controller.signal.aborted) setHistoryError(error.message || "版本记录暂时无法读取。"); }
    finally { if (!controller.signal.aborted) setHistoryBusy(false); }
  };
  return <section className="news-event-card" aria-label="事件证据与版本">
    <div className="news-event-heading"><h3><BookOpen size={17} aria-hidden="true" />事件证据</h3><span className={`news-event-state ${["full_text", "excerpt_only"].includes(state) ? "" : "news-event-warning"}`}>{state === "retracted" && evidence.withdrawal_evidence ? "用户确认来源撤回" : contentStateLabels[state] || "读取状态待确认"}</span></div>
    {!historical && event.last_fetch_attempt && ["fetch_failed", "access_restricted"].includes(event.last_fetch_attempt.content_status || event.last_fetch_attempt.status) ? <p className="news-event-warning">最近一次原文读取：{contentStateLabels[event.last_fetch_attempt.content_status || event.last_fetch_attempt.status]}。此前已保存的材料仍保留，不表示这次读取成功。</p> : null}
    {historical ? <p className="news-event-warning">正在查看历史版本 {version.version_number}；当前分析和市场资料不会回填到这个旧版本。<button className="news-inline-button" type="button" onClick={() => { setSelectedVersion(""); onHistoryChange?.(false, null); }}>返回当前版本</button></p> : null}
    {historical ? <div><h4>{version.title || "历史材料"}</h4><p>该版本原文发布时间：{eventTime(version.published_at)} · 首次发现：{eventTime(version.first_seen_at)}</p><p>{version.excerpt || "该版本没有摘录。"}</p></div> : null}
    {!historical && (event.requires_review || event.retracted) ? <p className="news-event-warning">{event.retracted ? "此事件有来源撤回记录，请核对下方确认依据。" : "材料有更正或更新。"}此前结论需要重新核对，旧记录仍保留。</p> : null}
    {evidence.withdrawal_evidence ? <details open><summary>用户确认的撤回依据</summary><blockquote>{evidence.withdrawal_evidence.quote}</blockquote><EvidenceLink url={evidence.withdrawal_evidence.url}>查看撤回公告原文</EvidenceLink><p>这是用户核对后保存的确认记录，尚未独立验证撤回含义与适用范围。</p></details> : null}
    {evidence.relation_confirmation ? <p className="news-event-caption">转载关系由用户核对后确认，保留所选原稿版本；该关联不等于独立事实验证。</p> : null}
    <dl className="news-event-facts"><div><dt>当前查看版本</dt><dd>{version.version_number || "尚未编号"}</dd></div><div><dt>直接关联标的</dt><dd>{symbols.length ? symbols.join("、") : "尚未确认"}</dd></div><div><dt>来源修订</dt><dd>{eventTime(version.source_updated_at)}</dd></div><div><dt>此版本首次发现</dt><dd>{eventTime(version.version_seen_at || item.version_seen_at)}</dd></div></dl>
    <p className="news-event-caption">时间显示为北京时间；发现时间不代表事件刚刚发生。</p>
    {changes.length ? <details className="news-event-changes" open><summary>这一版发生了什么变化</summary><ul>{changes.map((change, index) => <li key={index}><strong>{fieldLabels[change.field] || "证据更新"}</strong>{typeof change.before === "string" || typeof change.after === "string" ? <><p>此前：{String(change.before || "未提供")}</p><p>现在：{String(change.after || "未提供")}</p></> : <p>已有新记录，可展开正文与历史版本核对。</p>}</li>)}</ul></details> : <p className="news-event-caption">{version.version_number > 1 ? "内容版本已保存；具体新增事实仍需对照原文核实。" : "这是已保存的首个版本，尚无较早版本可对照。"}</p>}
    {preview || evidence.body_text || evidence.segments?.length || evidence.tables?.length ? <EvidencePreview preview={preview} versionId={historical ? version.id || selectedVersion : item.current_version_id || version.id} evidence={evidence} /> : null}
    <EvidenceText evidence={evidence} />
    <details className="news-event-attachments"><summary>附件读取情况 <span>{attachments.filter(a => a.read === true).length} / {attachments.length} 已取得文本（完整性另列）</span></summary>{attachments.length ? <ul>{attachments.map((attachment, index) => <li key={`${attachment.url}-${index}`}><EvidenceLink url={attachment.url}>{attachment.label || attachment.title || attachment.name || `附件 ${index + 1}`}</EvidenceLink><span>{attachmentReadingLabel(attachment)}</span>{attachmentReasonLabel(attachment) ? <small>{attachmentReasonLabel(attachment)}</small> : null}{attachment.read === true && attachment.evidence ? <details className="news-attachment-text"><summary>查看附件 {index + 1} 已保存文本</summary><EvidenceText evidence={attachment.evidence} /></details> : null}</li>)}</ul> : <p>未发现或尚未取得附件清单；正文已读不代表附件已读。</p>}</details>
    {!historical && typeof onRead === "function" ? <div className="news-event-read"><p>按当前消息的官方链接读取正文并保存版本。此操作不调用 AI；来源冷却与访问限制仍适用。</p><label><input type="checkbox" checked={includeAttachments} disabled={busy} onChange={e => setIncludeAttachments(e.target.checked)} />同时尝试读取公告中受支持的附件（最多 3 份）</label><button className="news-button" type="button" disabled={busy || event.retracted} onClick={() => onRead(includeAttachments)}>{busy ? <LoaderCircle size={16} className="news-spin" aria-hidden="true" /> : null}{busy ? "正在读取原文…" : "读取官方正文"}</button></div> : null}
    {historical ? <p><EvidenceLink url={version.url}>此历史版本记录的原文链接</EvidenceLink></p> : <details className="news-event-origins"><summary>来源与独立依据 <span>{origins.length} 处来源 · {Number.isInteger(event.independent_evidence_count) ? event.independent_evidence_count : "未知"} 份原始依据</span></summary><p>同一原稿的转载保留各自出处，不增加独立依据数量。</p><ul>{origins.map((origin, index) => <li key={origin.item_id || index}><EvidenceLink url={origin.url}>{origin.source_name || origin.source_id || "来源未标注"}</EvidenceLink><span>{origin.relation === "reprint" ? "转载" : origin.relation === "original" ? "原始来源" : "关联依据待核对"}</span>{origin.origin_url && origin.origin_url !== origin.url ? <EvidenceLink url={origin.origin_url}>追溯原始出处</EvidenceLink> : null}</li>)}</ul></details>}
    <button className="news-inline-button" type="button" aria-expanded={historyOpen} onClick={() => void loadHistory()}><History size={16} aria-hidden="true" />{historyOpen ? "收起版本历史" : `查看版本历史（${event.version_count || 1}）`}</button>
    {historyOpen ? <div className="news-event-history" aria-busy={historyBusy}>{historyBusy ? <p role="status">正在读取版本…</p> : historyError ? <p role="alert">{historyError}</p> : (versions || event.versions || []).map(row => <button key={row.id} className="news-button" type="button" aria-pressed={(selectedVersion || current.id) === row.id} onClick={() => { setSelectedVersion(row.id); onHistoryChange?.(row.id !== current.id, row.id !== current.id ? row : null); }}>版本 {row.version_number} · {eventTime(row.version_seen_at)}</button>)}</div> : null}
    {historical ? <section className="news-event-history-analysis" aria-label="此历史版本的分析记录"><h4>此历史版本的分析记录</h4>{version.ai_analysis ? <><p>{analysisStateLabel(version.ai_analysis)}</p>{["completed", "success"].includes(version.ai_analysis.status) ? <><p>{version.ai_analysis.summary}</p>{Array.isArray(version.ai_analysis.key_points) ? <ul>{version.ai_analysis.key_points.map((point,index) => <li key={index}>{point}</li>)}</ul> : null}<AnalysisEvidence analysis={version.ai_analysis} /></> : null}<AnalysisReview analysis={version.ai_analysis} source={{ ...version, current_version_id: version.id, evidence: version.evidence }} originalUrl={version.url} /></> : <p>此版本没有可读回的已保存分析记录。查看历史不会补跑模型。</p>}</section> : null}
    {!historical ? <NewsEventActions key={item.current_version_id} item={item} apiClient={apiClient} busy={busy} onUpdated={onUpdated} /> : null}
    {!historical ? <MarketImpact impact={impact && ["completed", "success"].includes(impact.status) ? impact : null} /> : null}
  </section>;
}

export function MarketImpact({ impact }) {
  const groups = [["facts", "已提供事实"], ["new_information", "新增信息"], ["inferred_impacts", "可能影响与条件"], ["counter_evidence", "反向解释"], ["unknowns", "仍缺证据"], ["follow_up_conditions", "后续验证条件"]];
  const comparison = typeof impact?.expectation_comparison === "string" ? impact.expectation_comparison : impact?.expectation_comparison?.claim;
  const reaction = typeof impact?.market_reaction === "string" ? impact.market_reaction : impact?.market_reaction?.claim;
  const lines = key => Array.isArray(impact?.[key]) ? impact[key].filter(value => typeof value === "string" || (value && typeof value.claim === "string")) : [];
  return <section className="news-event-market" aria-label="市场影响与待补证据"><h4>市场影响与待补证据</h4>
    <RevisionComparisonScope comparison={impact?.revision_comparison} />
    <p>{comparison || "尚未取得可核验的发布前市场预期，不能判断是否超预期。"}</p>
    <p>{reaction || "尚未提供可用的时点行情，市场反应待确认。"}</p>
    {strings(impact?.direct_symbols).length ? <p>直接关联标的：{impact.direct_symbols.join("、")}</p> : null}
    {impact?.market_asof || impact?.consensus_asof ? <p className="news-event-caption">行情时点：{eventTime(impact.market_asof)} · 预期时点：{eventTime(impact.consensus_asof)}</p> : null}
    {impact ? groups.map(([key, label]) => lines(key).length ? <div key={key}><h5>{label}</h5><ul>{lines(key).map((line, index) => <li key={index}>{typeof line === "string" ? line : <><p>{line.claim}</p>{line.condition ? <p>判断条件：{line.condition}</p> : null}{line.horizon ? <small>时间范围：{line.horizon}</small> : null}{line.quote ? <details><summary>查看这项判断的依据</summary><blockquote>{line.quote}</blockquote><p className="news-event-caption">引用记录：{line.evidence_id || "位置未标注"}</p></details> : null}</>}</li>)}</ul></div> : null) : null}
    <p className="news-event-caption">价格变化仅是观察到的反应；不能据此认定该消息已充分定价或造成全部波动。</p>
  </section>;
}

export function NewsQueueSummary({ queue, ai }) {
  if (!queue || typeof queue !== "object") return null;
  return <details className="news-event-queue"><summary>分析任务积压 <span>{queue.pending || 0} 待处理 · {queue.unknown || 0} 结果未确认</span></summary><p>{ai?.configured && ai?.automatic ? "自动分析按当前授权、预算和证据版本领取任务。" : "采集与分析分别运行。AI 尚未启用自动处理，待处理任务保留。"}未知结果不会自动重发。</p><dl>{Object.entries(taskLabels).filter(([key]) => queue[key] > 0).map(([key, label]) => <div key={key}><dt>{label}</dt><dd>{queue[key]}</dd></div>)}</dl></details>;
}

export function NewsQualitySummary({ quality }) {
  if (!quality) return null;
  const percent = value => Number.isFinite(value) ? `${(value * 100).toFixed(1)}%` : "暂无可计算样本";
  const seconds = value => Number.isFinite(value) ? `${value.toFixed(1)} 秒` : "暂无可计算样本";
  const latencyLabels = { publication_to_discovery: "来源发布 → 系统发现", discovery_to_body_ready: "发现 → 正文就绪", evidence_to_analysis_result: "证据版本 → 分析结果" };
  const judgedLabels = { duplicate_alert_rate: "重复提醒率", wrong_merge_rate: "错合并率", evidence_support_rate: "证据支持率", major_misstatement_count: "重大误述数", real_cost_per_effective_event: "单有效事件实际成本", relative_coverage_rate: "相对覆盖率" };
  return <details className="news-event-queue news-quality-panel"><summary>采集与分析质量记录 <span>仅当前已记录时段</span></summary>
    <p>统计窗口：{eventTime(quality.window_start)} 至 {eventTime(quality.window_end)}（北京时间）。这些运行记录不代替独立质量验收。</p>
    {Array.isArray(quality.source_checks) && quality.source_checks.length ? <div className="news-quality-table"><table><caption>来源检查：成功但无变化与失败分别记录</caption><thead><tr><th>来源</th><th>检查成功率</th><th>无变化</th><th>失败</th><th>未确认或进行中</th></tr></thead><tbody>{quality.source_checks.map(row => <tr key={row.source_id}><th>{row.name || row.source_id}</th><td>{percent(row.success_rate)}</td><td>{row.not_modified ?? "未知"}</td><td>{row.failed ?? "未知"}</td><td>{row.unknown_or_in_progress ?? "未知"}</td></tr>)}</tbody></table><p>成功率只以已经确认成功或失败的检查为分母；不等于消息完整覆盖率。</p></div> : null}
    <dl><div><dt>完整正文取得率</dt><dd>{percent(quality.body_reads?.full_text_rate)}</dd></div><div><dt>完整或部分正文取得率</dt><dd>{percent(quality.body_reads?.readable_text_rate)}</dd></div></dl>
    <div className="news-quality-table"><table><caption>处理延迟（只使用时间可靠的样本）</caption><thead><tr><th>环节</th><th>样本数</th><th>p50</th><th>p95</th></tr></thead><tbody>{Object.entries(latencyLabels).map(([key, label]) => <tr key={key}><th>{label}</th><td>{quality.latency?.[key]?.sample_count ?? "未知"}</td><td>{seconds(quality.latency?.[key]?.p50_seconds)}</td><td>{seconds(quality.latency?.[key]?.p95_seconds)}</td></tr>)}</tbody></table></div>
    <p>完整覆盖仍未确认；已知来源缺口 {quality.coverage?.known_source_gap_count ?? "未知"} 处、运行观察缺口 {quality.coverage?.known_monitor_gap_count ?? "未知"} 处。来源窗口不能证明停机期间全部消息已回补。</p>
    <details><summary>仍需独立评价的指标</summary><dl>{Object.entries(judgedLabels).map(([key, label]) => <div key={key}><dt>{label}</dt><dd>{quality.judged_metrics?.[key]?.value == null ? "证据不足，尚未评定" : String(quality.judged_metrics[key].value)}</dd></div>)}</dl></details>
  </details>;
}
