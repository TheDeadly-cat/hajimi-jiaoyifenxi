import React, { useEffect, useRef, useState } from "react";

export function NewsEventActions({ item, apiClient, busy, onUpdated }) {
  const [mode, setMode] = useState("relation");
  const [query, setQuery] = useState("");
  const [matches, setMatches] = useState([]);
  const [sourceId, setSourceId] = useState("");
  const [source, setSource] = useState(null);
  const [quote, setQuote] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  const [pending, setPending] = useState("");
  const [error, setError] = useState("");
  const active = useRef(true);
  const action = useRef(false);
  useEffect(() => { active.current = true; return () => { active.current = false; }; }, []);
  useEffect(() => {
    setMatches([]); setSourceId(""); setSource(null); setQuote(""); setAcknowledged(false); setError("");
    if (!query.trim()) return undefined;
    const controller = new AbortController();
    const timer = window.setTimeout(async () => {
      setPending("search");
      try {
        const result = await apiClient.newsItems({ query: query.trim(), limit: 50, offset: 0, signal: controller.signal });
        if (!controller.signal.aborted && active.current) setMatches((result.items || []).filter(row => mode === "withdrawal" || row.id !== item.id));
      } catch { if (!controller.signal.aborted && active.current) setError("已保存消息暂时无法检索。"); }
      finally { if (!controller.signal.aborted && active.current) setPending(""); }
    }, 250);
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [query, mode, item.id, apiClient]);
  useEffect(() => {
    setSource(null); setQuote(""); setAcknowledged(false);
    if (!sourceId) return undefined;
    const controller = new AbortController(); setPending("source");
    apiClient.newsItem(sourceId, controller.signal).then(result => { if (!controller.signal.aborted && active.current) setSource(result.item); }).catch(() => { if (!controller.signal.aborted && active.current) setError("所选消息详情暂时不可用。"); }).finally(() => { if (!controller.signal.aborted && active.current) setPending(""); });
    return () => controller.abort();
  }, [sourceId, apiClient]);
  if (typeof apiClient.newsEventRelation !== "function" || typeof apiClient.newsWithdrawal !== "function") return null;
  const sourceEvidence = source?.evidence || source?.event?.current_version?.evidence || {};
  const excerpts = [...(source?.excerpt ? [{ id: "excerpt", text: source.excerpt }] : []), ...(sourceEvidence.segments || [])].filter(row => typeof row.text === "string" && row.text.length > 0 && row.text.length <= 2000).slice(0, 50);
  const save = async () => {
    if (action.current || pending || busy || !source?.id || !acknowledged || (mode === "withdrawal" && !quote.trim())) return;
    action.current = true; setPending("save"); setError("");
    try {
      const result = mode === "relation" ? await apiClient.newsEventRelation(item.id, source.id) : await apiClient.newsWithdrawal(item.id, source.id, quote.trim());
      if (result.item?.id !== item.id) throw new Error("确认结果未取得，请重新读取消息状态。");
      if (active.current) { setAcknowledged(false); onUpdated?.(result.item); }
    } catch (reason) { if (active.current) setError(reason.message || "本次确认尚未保存。"); }
    finally { action.current = false; if (active.current) setPending(""); }
  };
  return <details className="news-event-actions"><summary>核对转载关系或来源撤回</summary><p>仅在核对已保存的来源后记录确认。系统会保留每篇原文、旧版本和旧结论，并注明这是用户确认。</p>
    <label>确认事项<select value={mode} disabled={!!pending || busy} onChange={event => setMode(event.target.value)}><option value="relation">这条消息转述同一份原稿</option><option value="withdrawal">来源已明确撤回这条材料</option></select></label>
    <label>{mode === "relation" ? "搜索已保存的原始公告标题" : "搜索已保存的撤回公告标题"}<input value={query} onChange={event => setQuery(event.target.value)} maxLength={200} disabled={pending === "save" || busy} placeholder="输入标题关键词，仅检索本机已保存消息" /></label>
    {pending === "search" ? <p role="status">正在检索已保存消息…</p> : null}
    {matches.length ? <label>选择对应的已保存消息<select value={sourceId} disabled={!!pending || busy} onChange={event => setSourceId(event.target.value)}><option value="">请选择消息</option>{matches.map(row => <option key={row.id} value={row.id}>{row.title} · {row.source_name}</option>)}</select></label> : query.trim() && !pending ? <p>没有找到可用的已保存消息；请调整标题关键词。</p> : null}
    {source ? <div><p>已选：<strong>{source.title}</strong> · {source.source_name}</p>{mode === "withdrawal" ? <><p>访问失败或网页消失不能证明撤回。请选择来源明确表达撤回的原句，必要时缩短到其中的连续原文。</p><label>选择已保存原文段落<select value="" disabled={!!pending || busy} onChange={event => { const row = excerpts.find(entry => entry.id === event.target.value); setQuote(row?.text || ""); setAcknowledged(false); }}><option value="">选择一段已保存的原文</option>{excerpts.map((row, index) => <option key={`${row.id}-${index}`} value={row.id}>{row.text.slice(0, 90)}{row.text.length > 90 ? "…" : ""}</option>)}</select></label><label>确认撤回所依据的原句<textarea value={quote} maxLength={2000} rows={4} disabled={!!pending || busy} onChange={event => { setQuote(event.target.value); setAcknowledged(false); }} /></label></> : <p>确认后将它们关联到同一事件，并保留各自出处。这不代表两篇报道互相独立验证。</p>}
      <label className="news-event-ack"><input type="checkbox" checked={acknowledged} disabled={!!pending || busy} onChange={event => setAcknowledged(event.target.checked)} /><span>{mode === "relation" ? "我已核对原文，确认本条转述的是所选原稿中的同一事件。" : "我已核对所选公告与原句，确认它明确撤回了当前材料。"}</span></label>
      <button className="news-button" type="button" disabled={!!pending || busy || !acknowledged || (mode === "withdrawal" && !quote.trim())} onClick={() => void save()}>{pending === "save" ? "正在保存确认…" : mode === "relation" ? "保存用户确认的转载关系" : "保存用户确认的来源撤回"}</button></div> : null}
    {error ? <p role="alert" className="news-source-error">{error}</p> : null}
  </details>;
}
