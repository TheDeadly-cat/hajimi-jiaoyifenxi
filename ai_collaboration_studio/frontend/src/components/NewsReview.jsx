import { useEffect, useState } from "react";
import { api } from "../api";
import "./NewsReview.css";

const states = {
  UNREVIEWED: "未审核", WAITING_DOCUMENT: "等待读取正文", READING_DOCUMENT: "正在读取正文",
  QUEUED: "等待主审", RUNNING: "主审处理中", REVIEWED: "主审已返回 · 事实尚未独立核验",
  MATERIAL_INSUFFICIENT: "材料不足", FAILED: "审核失败 · 已停止后续调用",
  UNKNOWN: "结果未知 · 不会自动重发", CANCELLED: "审核已取消",
  DOCUMENT_CANCELLED: "正文读取已取消 · 尚未判断材料是否充足",
  WAITING_AUTHORIZATION: "旧授权已失效 · 未发送审核等待新的授权",
};
const importanceLabels = { high: "优先关注", normal: "普通关注", uncertain: "重要性待判断" };
const reasons = {
  model_budget_exhausted: "审核预算已用尽，继续保留新消息。",
  unknown_result: "有请求的结果或用量未知，付费审核已停止。",
  review_failure: "审核未通过结果校验，付费审核已停止。",
  operator_pause: "已暂停新采集、正文读取和审核；已经发出的请求可能仍在返回。",
  restart_confirmation_required: "重启后等待确认原授权。",
};
function date(value) {
  if (!value) return "未知";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? "未知" : parsed.toLocaleString("zh-CN");
}

export function NewsReview({ itemId }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    const abort = new AbortController();
    let timer;
    const read = async () => {
      try {
        const response = await api.sourceNewsReview(itemId, abort.signal);
        if (abort.signal.aborted) return;
        const next = response.news_review;
        if (next?.version !== "news_event_review_v1" || next.item_id !== itemId
          || !Array.isArray(next.reviews) || !states[next.state]) throw new Error("消息审核记录不匹配，无法显示。");
        setData(next); setError("");
        if (next.observation_until > Date.now()) timer = setTimeout(read, 10_000);
      } catch (err) {
        if (!abort.signal.aborted) setError(err.message || "审核状态读取失败。");
      }
    };
    void read();
    return () => { abort.abort(); clearTimeout(timer); };
  }, [itemId, refresh]);
  const current = data?.item_id === itemId ? data : null;
  const review = current?.reviews.find((value) => value.id === current.current_review_id)
    || current?.reviews.findLast((value) => value.document_version_id === current.current_document_version_id)
    || current?.reviews.at(-1);
  const history = current?.reviews.filter((value) => value.id !== review?.id) || [];
  const result = review?.receipt?.result;
  const coverage = current?.coverage || review?.coverage;
  const importance = current?.importance || review?.importance;
  return <section className="source-inbox-section news-review" aria-label="自动消息审核">
    <h3>自动消息审核</h3>
    <p role="status">{states[current?.state] || "正在读取审核状态…"}</p>
    {error ? <p role="alert">{error} 当前状态尚未确认。</p> : null}
    <button type="button" className="secondary compact" onClick={() => setRefresh((value) => value + 1)}>刷新审核状态</button>
    {current ? <>
      <dl className="news-review-facts">
        <div><dt>重点标记</dt><dd>{importanceLabels[importance?.level] || "重要性待判断"}；{importance?.reasons?.join("；")}</dd></div>
        <div><dt>影响标的</dt><dd>{importance?.targets?.join("、") || "范围待判断"}</dd></div>
        <div><dt>发布时间</dt><dd>{date(current.freshness?.published_at)}</dd></div>
        <div><dt>发现时间</dt><dd>{date(current.freshness?.discovered_at)}</dd></div>
        <div><dt>发布时间距今</dt><dd>{({ older_than_24h: "已超过 24 小时", within_24h: "在 24 小时内", unknown: "未知" }[current.freshness?.publication_age] || "未知")}</dd></div>
        {current.freshness?.modified_at ? <div><dt>来源标注的更新时间</dt><dd>{date(current.freshness.modified_at)}</dd></div> : null}
      </dl>
      {current.freshness?.clock_anomaly ? <p>发布与发现时间存在异常，不能据此判断及时性。</p> : null}
      {coverage ? <>
        <h4>材料范围</h4>
        <p>{coverage.scope}；读取 {coverage.paragraph_count ?? "未知"} 段。</p>
        <p>附件：{coverage.attachment_reading === "not_read" ? "未读取" : "状态未知"}。</p>
        <ul>{coverage.warnings?.map((warning) => <li key={warning}>{warning}</li>)}</ul>
      </> : <p>尚无可展示的正文审核。未读取成功不代表没有重要消息。</p>}
      {result ? <>
        <h4>主审意见</h4>
        {current.current_document_version_id && current.current_document_version_id !== review.document_version_id
          ? <p>以下意见使用此前保存的正文版本，不能代替对当前版本的审核。</p> : null}
        <p>{result.summary}</p>
        <p>主审对重要性的判断：{importanceLabels[result.importance?.level] || "待判断"}。{result.importance?.reason}</p>
        <h4>有原文引用的陈述</h4>
        {result.facts.length ? result.facts.map((fact, index) => <div key={`${fact.paragraph_id}-${index}`}>
          <p>{fact.claim}</p><blockquote>{fact.quote}</blockquote>
          <details><summary>引用定位</summary><p>{fact.paragraph_id}</p></details>
        </div>) : <p>未形成有原文引用的陈述。</p>}
        <h4>推断与局限</h4>
        {result.inferences.length ? <ul>{result.inferences.map((value, index) => <li key={index}>{value.claim}（{value.limitations}）</li>)}</ul> : <p>未提出额外推断。</p>}
        <h4>反证与待核对问题</h4>
        {result.counterevidence.length ? <ul>{result.counterevidence.map((value, index) => <li key={index}>{value.claim}</li>)}</ul> : <p>主审未指出原文反证；这不表示不存在反证。</p>}
        <ul>{result.open_questions.map((value, index) => <li key={index}>{value}</li>)}</ul>
        <ul>{result.limitations.map((value, index) => <li key={index}>{value}</li>)}</ul>
        <p>以上为未独立核验的模型意见。调用完成和引用匹配不等于事实确认。</p>
      </> : null}
      {history.length ? <details><summary>其他审核版本（{history.length}）</summary>
        <ul>{history.map((value) => <li key={value.id}>{states[value.state]}<p>{value.receipt?.result?.summary || "无有效主审意见"}</p><small>{value.document_version_id}</small></li>)}</ul>
      </details> : null}
    </> : null}
  </section>;
}

export function NewsReviewControl() {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    const abort = new AbortController();
    let timer;
    const read = async () => {
      try {
        const response = await api.newsReviewControl(abort.signal);
        if (abort.signal.aborted) return;
        if (!response.news_review?.state) throw new Error("运行状态格式无效。");
        setData(response.news_review); setError("");
        if (response.news_review.state === "ACTIVE" && !response.news_review.expired) timer = setTimeout(read, 10_000);
      } catch (err) { if (!abort.signal.aborted) setError(err.message); }
    };
    void read();
    return () => { abort.abort(); clearTimeout(timer); };
  }, [refresh]);
  const pause = async () => {
    setBusy(true);
    try { const response = await api.pauseNewsReview(); setData(response.news_review); setRefresh((value) => value + 1); }
    catch (err) { setError(err.message); }
    finally { setBusy(false); }
  };
  const stateLabel = data?.expired ? "本次运行授权已到期"
    : Object.keys(data?.worker_errors || {}).length ? "后台处理中断"
      : data?.stopping ? "后台正在停止"
        : data?.state === "ACTIVE" && data?.paid_stop_reason ? "授权窗口内 · 付费审核已停止"
          : ({ ACTIVE: "授权窗口内", PAUSED: "已暂停", STOPPED: "已停止", DISABLED: "尚未启用" }[data?.state] || "正在读取…");
  return <section className="source-inbox-section news-review" aria-label="自动审核运行状态">
    <div className="news-review-control-heading">
      <h3>自动审核</h3><p role="status">{stateLabel}</p>
      {data?.state === "ACTIVE" && !data.expired ? <button type="button" className="secondary compact" disabled={busy} onClick={pause}>暂停本次自动采集与审核</button> : null}
    </div>
    {error ? <p role="alert">{error} 尚不能确认运行状态。</p> : null}
    <details><summary>查看本次范围与用量</summary>
    {data?.policy ? <>
      <p>NVDA 8-K 与 Micron 公告，目标每 5 分钟检查，来源限流时会延后。到期：{date(data.policy.expires_at_ms)}。</p>
      <p>正文预约 {data.documents_reserved}/{data.policy.max_document_requests}；模型预约 {data.calls_reserved}/{data.policy.max_model_calls}；预留费用 ¥{data.cost_reserved_cny} / ¥{data.policy.spend_limit_cny}（估算，非账单）。</p>
      <p>新事件 {data.events_observed}；待主审 {data.job_counts?.QUEUED || 0}；结果未知 {data.job_counts?.UNKNOWN || 0}。</p>
      {data.paid_stop_reason ? <p>{reasons[data.paid_stop_reason] || "审核已停止，需要核对运行记录。"}</p> : null}
      {data.observation === "no_new_event_observed" ? <p>尚未观察到新增事件，尚不能据此证明完整链路已在真实消息上运行。</p> : null}
    </> : <p>需要明确限定来源、期限和预算的运行授权后，才会自动读取正文并交给主审模型。</p>}
    <button type="button" className="secondary compact" onClick={() => setRefresh((value) => value + 1)}>刷新运行状态</button>
    </details>
  </section>;
}
