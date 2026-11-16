import React, { useEffect, useRef, useState } from "react";
import { eventLink, eventTime, MarketImpact } from "./NewsEventCard";

const PAGE_SIZE = 50;
const modes = {
  current_news_only: "当时的消息解释",
  current: "结合已保存资料的分析",
  historical_replay: "历史回放",
};
const completed = new Set(["completed", "success"]);
const validRecord = value => value && typeof value.context_id === "string" && value.context_id
  && typeof value.article_version_id === "string" && value.article_version_id
  && Object.hasOwn(modes, value.mode);
const versionLabel = record => Number.isInteger(record.article_version_number) && record.article_version_number > 0
  ? `版本 ${record.article_version_number}` : "已保存版本";

export function NewsSavedAnalyses({ item, versionId = null, apiClient, busy = false }) {
  const [open, setOpen] = useState(false);
  const [page, setPage] = useState(null);
  const [selected, setSelected] = useState(null);
  const [pending, setPending] = useState("");
  const [error, setError] = useState("");
  const active = useRef(true);
  const request = useRef(null);
  const sequence = useRef(0);
  const actionBusy = useRef(false);
  useEffect(() => {
    active.current = true;
    setOpen(false); setPage(null); setSelected(null); setPending(""); setError("");
    actionBusy.current = false;
    return () => { active.current = false; request.current?.abort(); sequence.current++; };
  }, [item.id, item.current_version_id, versionId]);

  if (typeof apiClient.newsMarketHistory !== "function" || typeof apiClient.newsMarketSavedContext !== "function") return null;
  const start = kind => {
    request.current?.abort();
    const controller = new AbortController(), token = ++sequence.current;
    request.current = controller; actionBusy.current = true; setPending(kind); setError("");
    return { controller, token };
  };
  const current = action => active.current && !action.controller.signal.aborted && action.token === sequence.current;
  const finish = action => { if (current(action)) { actionBusy.current = false; setPending(""); } };
  const load = async (append = false) => {
    if (busy || actionBusy.current) return;
    setOpen(true);
    const action = start("list"), offset = append ? page.nextOffset : 0;
    try {
      const value = await apiClient.newsMarketHistory(item.id, versionId, action.controller.signal, { limit: PAGE_SIZE, offset });
      if (!Array.isArray(value.contexts) || !Number.isInteger(value.total) || value.total < 0
          || value.offset !== offset || value.limit !== PAGE_SIZE
          || value.contexts.some(record => !validRecord(record) || (versionId && record.article_version_id !== versionId))
          || (value.total > offset && value.contexts.length === 0)) {
        throw new Error("保存记录的范围尚未确认，请重新读取；已有分析不会被覆盖。");
      }
      if (current(action)) {
        setPage({ records: append ? [...page.records, ...value.contexts.filter(record => !page.records.some(prior => prior.context_id === record.context_id))] : value.contexts,
          total: value.total, nextOffset: offset + value.contexts.length });
        if (!append) setSelected(null);
      }
    } catch (reason) {
      if (current(action)) setError(reason.message || "保存记录暂时无法读取，请重试。");
    } finally { finish(action); }
  };
  const read = async record => {
    if (busy || actionBusy.current) return;
    const action = start(record.context_id); setSelected(null);
    try {
      const value = await apiClient.newsMarketSavedContext(item.id, record.context_id, action.controller.signal, { version_id: record.article_version_id });
      const saved = value.saved_record, context = value.market_context, impact = value.market_impact;
      if (!validRecord(saved) || saved.context_id !== record.context_id || saved.article_version_id !== record.article_version_id
          || context?.context_id !== record.context_id || (impact && impact.context_id !== record.context_id)) {
        throw new Error("返回的分析与所选保存版本不一致，暂不展示，请重新读取。");
      }
      if (current(action)) setSelected({ record: saved, context, impact });
    } catch (reason) {
      if (current(action)) setError(reason.message || "这份分析暂时无法读取，请重试。");
    } finally { finish(action); }
  };
  const saved = selected?.record, impact = selected?.impact;
  const originalUrl = saved ? eventLink(saved.source_url) : "";
  return <section className="news-saved-analyses" aria-label="已保存的分析">
    <h4>{versionId ? "此版本保存的分析" : "已保存的分析"}</h4>
    <p>按原来的证据版本和时间查看。读取记录不会调用 AI，也不会把旧行情更新成当前行情。</p>
    <button className="news-inline-button" type="button" aria-expanded={open} disabled={busy || !!pending}
      onClick={() => { if (open) setOpen(false); else void load(); }}>
      {open ? "收起已保存分析" : "查看已保存的分析"}
    </button>
    {open ? <div aria-busy={!!pending}>
      {pending === "list" ? <p role="status">正在读取保存记录…</p> : null}
      {error ? <p className="news-source-error" role="alert">{error}</p> : null}
      {page && !page.total && !error && pending !== "list" ? <p>这里还没有保存的分析资料。查看记录不会补跑模型。</p> : null}
      {page?.records.length ? <ul>{page.records.map(record => <li key={record.context_id}>
        <button className="news-button" type="button" disabled={busy || !!pending} aria-pressed={saved?.context_id === record.context_id}
          onClick={() => void read(record)}>
          {versionLabel(record)} · {modes[record.mode]} · 资料保存于 {eventTime(record.created_at)}
        </button>
      </li>)}</ul> : null}
      {page && page.nextOffset < page.total ? <button className="news-inline-button" type="button" disabled={busy || !!pending} onClick={() => void load(true)}>加载更多保存记录</button> : null}
      <button className="news-inline-button" type="button" disabled={busy || !!pending} onClick={() => void load()}>刷新保存记录</button>
      {pending && pending !== "list" ? <p role="status">正在读取所选分析…</p> : null}
      {selected ? <article className="news-saved-analysis" aria-label="所选保存分析">
        <h5>{versionLabel(saved)} · {modes[saved.mode]}</h5>
        {saved.is_shared ? <p className="news-event-caption">这份分析最初保存于同源消息记录；当前展示的是所选消息中证据一致的版本，原分析归属和内容保持不变。</p> : null}
        {saved.title ? <p>{saved.title}</p> : null}
        <p>资料保存：{eventTime(saved.created_at)} · 分析材料截止：{eventTime(saved.as_of)}</p>
        <p className="news-event-caption">这是按当时材料保存的记录。与当前新闻证据相同，也不代表当时的行情或预期仍然有效。</p>
        {saved.evidence_matches_current === false ? <p className="news-event-warning">消息已有不同版本。以下是当时保存的分析，不能作为当前结论。</p>
          : saved.evidence_matches_current !== true ? <p className="news-event-warning">这份记录与当前消息版本的关系尚未确认。</p> : null}
        {saved.requires_review ? <p className="news-event-warning">相关材料已有变化，此前判断需要重新核对；原结果和引用保持原样。</p> : null}
        {originalUrl ? <a href={originalUrl} target="_blank" rel="noopener noreferrer">打开此保存版本记录的原文链接</a> : null}
        <p className="news-event-caption">原文网页可能已更新；保存的原句仍列在分析依据中，旧版正文可在消息版本历史中核对。</p>
        {impact && completed.has(impact.status) ? <MarketImpact impact={impact} />
          : <p role="status">{impact?.status === "unknown" ? "这份分析的结果未确认，记录仍保留，查看不会重新发送。"
            : impact?.material_insufficient ? "当时材料不足，没有发送模型请求。"
            : ["queued", "pending", "running"].includes(impact?.status) ? "这份记录的任务尚未完成；此处只读取状态，不重新排队。"
            : impact ? "这份分析尚无可确认的完成结果，记录仍保留。" : "这份资料尚无已保存的分析结果。"}</p>}
      </article> : null}
    </div> : null}
  </section>;
}
