import { useEffect, useRef, useState } from "react";

const emptyFocus = { all_registered: true, companies: [], symbols: [], industries: [], macro_topics: [], automatic_scope: "market" };
const list = value => Array.isArray(value) ? value : [];
const copyFocus = value => ({ ...emptyFocus, ...value, companies: [...list(value?.companies)], symbols: [...list(value?.symbols)], industries: [...list(value?.industries)], macro_topics: [...list(value?.macro_topics)] });
const validMode = value => ["collect", "manual", "automatic"].includes(value);
const normalizedSymbol = value => String(value || "").toUpperCase().replace(/^US\./, "");
export function focusSourcePlan(focus, catalog, overview) {
  const sources = list(overview?.sources);
  if (!focus || !Array.isArray(overview?.sources)) return { matched: [], enable: [], blocked: [], unmatched: [] };
  const selectedIds = new Set();
  const unmatched = [];
  if (!focus.all_registered) {
    for (const kind of ["companies", "symbols", "industries", "macro_topics"]) {
      if (kind !== "symbols" && !Array.isArray(catalog?.[kind])) continue;
      for (const id of list(focus[kind])) {
        const option = list(catalog?.[kind]).find(row => row.id === id);
        const ids = new Set(list(option?.source_ids));
        const symbols = new Set(kind === "symbols" ? [normalizedSymbol(id)] : kind === "companies" && option?.symbol ? [normalizedSymbol(option.symbol)] : []);
        if (kind === "industries") for (const company of list(catalog?.companies)) {
          if (list(company.industries).includes(id)) {
            for (const sourceId of list(company.source_ids)) ids.add(sourceId);
            if (company.symbol) symbols.add(normalizedSymbol(company.symbol));
          }
        }
        const matches = sources.filter(source => ids.has(source.id) || list(source.symbols).some(symbol => symbols.has(normalizedSymbol(symbol))));
        if (!matches.length) unmatched.push({ kind, id, label: kind === "symbols" ? normalizedSymbol(id) : option?.label || id });
        for (const source of matches) selectedIds.add(source.id);
      }
    }
  }
  const matched = sources.filter(source => focus.all_registered ? source.enabled !== false : selectedIds.has(source.id));
  const blocked = matched.filter(source => source.enabled === false && (source.manual_hold || source.can_configure === false || (source.source_kind === "sec" && overview?.sec_contact_configured !== true)));
  const enable = matched.filter(source => source.enabled === false && !blocked.includes(source));
  return { matched, enable, blocked, unmatched };
}
const unmatchedMessage = entries => entries.map(entry => `${entry.label}暂无已登记来源，本次不会监控它`).join("；") + "。";
const pauseLabels = {
  authorization_required: "主模型尚未设置，仍可收集和阅读", manual_mode: "手动模式，选择消息后再确认解释", monitor_paused: "消息监控已暂停，自动解释等待恢复监控",
  not_configured: "主模型尚未设置，仍可收集和阅读", authorization_expired: "本次授权已到期，请重新设置主模型",
  expired: "本次授权已到期，请重新设置主模型", budget_exhausted: "预约额度已用完，等待额度释放",
  call_limit: "调用次数已达到上限，等待额度释放", manual: "手动模式，选择消息后再确认解释",
  collection_paused: "消息监控已暂停，自动解释等待恢复监控", monitoring_paused: "消息监控已暂停，自动解释等待恢复监控",
};
function amount(value, fallback = "未知") { return Number.isFinite(value) ? Number(value).toFixed(2) : fallback; }
function count(value) { return Number.isInteger(value) && value >= 0 ? value : "未知"; }
function date(value) { const parsed = value ? new Date(value) : null; return parsed && Number.isFinite(parsed.getTime()) ? parsed.toLocaleString("zh-CN", { hour12: false }) : "待确认"; }
function backlogReason(value) {
  const reason = typeof value === "string" ? value : value?.reason;
  return reason === "no_eligible_backlog_within_budget" ? "按当前关注、证据条件和本批预算，没有可补处理的旧待办。请核对待办条件或调整本批上限；已完成、失败和结果未知的记录仍不会重发。" : "部分旧待办尚不满足本次处理条件，请核对关注范围、可引用证据和剩余额度。已有记录保留，不会自动重发。";
}

export function focusDescription(value, catalog = {}) {
  const focus = value?.focus || value?.current || value || {};
  if (focus.all_registered === true) return "全部已启用来源中的消息";
  const names = ["companies", "industries", "macro_topics"].flatMap(key => list(focus[key]).map(id => list(catalog[key]).find(row => row.id === id)?.label || id));
  names.push(...list(focus.symbols));
  return names.length ? names.join("、") : "关注范围待设置";
}

export function NewsFlowStatus({ overview, ai, runningLabel, error, routeLabel, focus, catalog, onSetup, onAISetup }) {
  const pending = ai?.pending || {};
  const plan = focusSourcePlan(focus?.focus || focus?.current || focus, catalog, overview);
  const expiry = ai?.expires_at;
  const reason = ai?.error || pauseLabels[ai?.pause_reason] || (ai?.pause_reason ? "暂不能自动处理，请查看设置和待办" : "");
  const ready = ai?.configured === true;
  const modelStatus = error || typeof ai?.configured !== "boolean" ? "状态待确认" : ai?.pause_reason === "authorization_expired" ? "授权到期，需重新设置" : !ready ? "尚未启用" : ai?.pause_reason === "budget_exhausted" ? "预约额度已用完" : ai.automatic && ["monitor_paused", "monitoring_paused", "collection_paused"].includes(ai.pause_reason) ? "等待恢复消息监控" : ai.automatic ? "自动解释重要变化" : "手动解释";
  return <section className="news-flow-overview" aria-label="消息处理三阶段">
    <div className="news-flow-heading"><p><strong>我的关注：</strong>{focusDescription(focus, catalog)}</p><button type="button" className="news-inline-button" onClick={onSetup}>设置关注与工作方式</button></div>
    {plan.unmatched.length ? <div className="news-flow-next news-history-warning" role="status">{unmatchedMessage(plan.unmatched)}</div> : null}
    <div className="news-flow-stages">
      <article><span className="news-flow-step">1 · 消息监控</span><strong>{runningLabel}</strong><p>收集已启用来源，保留消息和时间。</p></article>
      <article><span className="news-flow-step">2 · 重要原文</span><strong>{error ? "状态待确认" : overview?.article_reading?.enabled === true ? overview?.enabled === true ? "自动补充已开启" : "已设置，等待恢复监控" : overview?.article_reading?.enabled === false ? "未开启自动补充" : "读取设置待确认"}</strong><p>正文待处理 {count(overview?.article_reading?.queue?.pending)} 条；附件需单独确认。</p></article>
      <article><span className="news-flow-step">3 · AI 分析</span><strong>{modelStatus}</strong><p>{ready ? `主模型：${routeLabel}；${ai.automatic ? "依关注与证据筛选" : "选一条再确认发送"}。` : "可以先免费阅读，之后再设置一个主模型。"}</p></article>
    </div>
    <div className="news-flow-next"><span>{error ? "状态读取失败，先重新读取；这里保留上次记录。" : reason || (ready ? ai.automatic ? "只处理符合策略的重要新变化；重复、无关和旧待办另列。" : "下一步：打开一条消息，核对来源后选择解释。" : "下一步：开始监控或阅读已保存消息；AI 可稍后设置。")}</span>{!ready ? <button type="button" className="news-inline-button" onClick={onAISetup}>设置主模型</button> : null}</div>
    {ai ? <details className="news-flow-budget"><summary>授权与待办：待处理 {count(pending.total ?? overview?.analysis_queue?.pending)} 条 · 结果未知 {count(pending.unknown ?? overview?.analysis_queue?.unknown)} 条</summary><dl><div><dt>剩余预约次数</dt><dd>{count(ai.remaining_calls ?? (Number.isInteger(ai.max_calls) && Number.isInteger(ai.calls_reserved) ? Math.max(0, ai.max_calls - ai.calls_reserved) : undefined))}</dd></div><div><dt>剩余预约估算</dt><dd>{amount(ai.remaining_cny ?? (Number.isFinite(ai.budget_cny) && Number.isFinite(ai.reserved_cny) ? Math.max(0, ai.budget_cny - ai.reserved_cny) : undefined))} 元</dd></div><div><dt>本次授权到期</dt><dd>{ready ? date(expiry) : "未授权"}</dd></div><div><dt>下一笔额度释放</dt><dd>{date(ai.budget_resets_at)}</dd></div></dl><p>可自动处理 {count(pending.eligible)} 条 · 等待条件 {count(pending.held)} 条 · 旧待办 {count(pending.backlog)} 条。预约是估算，实际扣费以供应商账单为准。</p><p>重启后需重新设置主模型；切换手动／自动不会延长原授权，也不会清空已用额度。结果未知的请求保留，不自动重发。</p></details> : null}
  </section>;
}

function presetFor(focus) {
  if (focus.all_registered) return "all";
  const selected = ["companies", "industries", "macro_topics"].flatMap(key => focus[key].map(id => `${key}:${id}`));
  return selected.length === 1 && focus.symbols.length === 0 ? selected[0] : "custom";
}

export function NewsSetupFlow({ apiClient, overview, ai, routeLabel, pending, setupRef, onApply, onAISetup, onFocusLoaded }) {
  const [focus, setFocus] = useState(null);
  const [catalog, setCatalog] = useState({});
  const [symbolText, setSymbolText] = useState("");
  const [error, setError] = useState("");
  const [mode, setMode] = useState("collect");
  const [readOriginal, setReadOriginal] = useState(false);
  const [acknowledged, setAcknowledged] = useState(false);
  const [edited, setEdited] = useState(false);
  const initialized = useRef(false);
  const generation = useRef(0);
  useEffect(() => {
    if (typeof apiClient.newsFocus !== "function") return undefined;
    const controller = new AbortController(); const current = ++generation.current;
    apiClient.newsFocus(controller.signal).then(result => {
      if (controller.signal.aborted || current !== generation.current) return;
      if (!result.focus || typeof result.focus.all_registered !== "boolean" || !result.catalog) throw new Error("关注设置尚未确认。");
      setFocus(copyFocus(result.focus)); setSymbolText(list(result.focus.symbols).join(" ")); setCatalog(result.catalog); setError(""); onFocusLoaded?.(result);
    }).catch(reason => { if (!controller.signal.aborted && current === generation.current) setError(reason.message || "关注设置暂时无法读取。"); });
    return () => controller.abort();
  }, [apiClient, onFocusLoaded]);
  useEffect(() => {
    if (initialized.current || !overview || !ai) return;
    initialized.current = true;
    setMode(ai.configured ? ai.automatic ? "automatic" : "manual" : "collect");
    setReadOriginal(overview.article_reading?.enabled === true);
    if (overview.enabled === false && overview.counts?.total === 0 && !ai.configured && setupRef?.current) setupRef.current.open = true;
  }, [overview, ai, setupRef]);
  if (typeof apiClient.newsFocus !== "function" || typeof apiClient.newsFocusSave !== "function") return null;
  const choosePreset = value => {
    setEdited(true); setAcknowledged(false);
    if (value === "custom") { setFocus(previous => ({ ...previous, all_registered: false })); return; }
    const next = copyFocus({ automatic_scope: focus?.automatic_scope || "market" });
    if (value !== "all") { const [key, id] = value.split(":"); if (["companies", "industries", "macro_topics"].includes(key)) { next.all_registered = false; next[key] = [id]; } }
    setFocus(next); setSymbolText("");
  };
  const changeList = (key, id, checked) => { setEdited(true); setAcknowledged(false); setFocus(previous => ({ ...previous, all_registered: false, [key]: checked ? [...new Set([...previous[key], id])] : previous[key].filter(value => value !== id) })); };
  const configured = ai?.configured === true;
  const sourcePlan = focusSourcePlan(focus, catalog, overview);
  const empty = focus && !focus.all_registered && !["companies", "symbols", "industries", "macro_topics"].some(key => focus[key].length);
  const needsAI = mode !== "collect";
  const disabled = !!pending || !focus || !overview || empty || !sourcePlan.matched.length || sourcePlan.blocked.length === sourcePlan.matched.length || (needsAI && !configured) || (mode === "automatic" && !acknowledged);
  return <details ref={setupRef} className="news-setup-flow">
    <summary><strong>首次设置／调整关注</strong><span>选关注 → 选工作方式 → 开始监控</span></summary>
    <form onSubmit={event => { event.preventDefault(); if (!disabled && validMode(mode)) { setAcknowledged(false); void onApply({ focus: copyFocus(focus), mode, readOriginal, sourcePlan }).then(saved => { if (saved) { setEdited(false); if (setupRef.current) setupRef.current.open = false; } }); } }}>
      <div className="news-setup-fields"><label><strong>1. 我关注什么</strong><select aria-label="关注范围" value={focus ? presetFor(focus) : "loading"} disabled={!!pending || !focus} onChange={event => choosePreset(event.target.value)}>{!focus ? <option value="loading">正在读取已保存关注…</option> : null}<option value="all">全部已启用来源</option>{["companies", "industries", "macro_topics"].map(key => <optgroup key={key} label={key === "companies" ? "公司与股票" : key === "industries" ? "行业" : "宏观主题"}>{list(catalog[key]).map(row => <option value={`${key}:${row.id}`} key={row.id}>{row.label}{row.symbol ? `（${row.symbol}）` : ""}</option>)}</optgroup>)}<option value="custom">自定义多个关注对象</option></select></label>
      <label><strong>2. 希望系统做多少</strong><select aria-label="工作方式" value={mode} disabled={!!pending} onChange={event => { setMode(event.target.value); if (event.target.value === "automatic") setFocus(previous => previous ? { ...previous, automatic_scope: "market" } : previous); setAcknowledged(false); setEdited(true); }}><option value="collect">只收集阅读（免费）</option><option value="manual">手动解释一条消息</option><option value="automatic">自动解释重要变化</option></select></label></div>
      {focus && presetFor(focus) === "custom" ? <fieldset className="news-focus-custom" disabled={!!pending}><legend>选择关注对象</legend>{["companies", "industries", "macro_topics"].map(key => <div key={key}>{list(catalog[key]).map(row => <label key={row.id}><input type="checkbox" checked={focus[key].includes(row.id)} onChange={event => changeList(key, row.id, event.target.checked)} />{row.label}</label>)}</div>)}<label>股票代码（用空格或逗号分隔）<input aria-label="关注股票代码" value={symbolText} onChange={event => { setSymbolText(event.target.value); setEdited(true); setFocus(previous => ({ ...previous, symbols: [...new Set(event.target.value.toUpperCase().split(/[\s,，]+/).filter(Boolean))] })); }} maxLength={150} placeholder="例如 MU SNDK WDC STX" /></label></fieldset> : null}
      <div className="news-setup-source-plan"><p>所选关注对应的已登记来源：{sourcePlan.matched.map(row => row.name || row.id).join("、") || "暂未匹配到来源"}。</p><p>{sourcePlan.enable.length ? `点击下方开始按钮会启用：${sourcePlan.enable.map(row => row.name || row.id).join("、")}，并按各来源期限访问官方页面。` : "此次没有新来源需要启用。"}保存关注本身不读取来源；其他已启用来源保持原设置。</p>{sourcePlan.unmatched.length ? <p className="news-history-warning" role="status">{unmatchedMessage(sourcePlan.unmatched)}</p> : null}{sourcePlan.blocked.length ? <p className="news-history-warning">暂不启用：{sourcePlan.blocked.map(row => `${row.name || row.id}（${row.source_kind === "sec" && overview?.sec_contact_configured !== true ? "需先填写 SEC 联系标识" : "需人工核对来源状态"}）`).join("、")}。这些来源仍未覆盖。</p> : null}{!sourcePlan.matched.length && focus ? <p className="news-source-error">当前没有与所选关注对应的已登记来源。请调整关注范围，或在“来源状态”核对可用来源。</p> : null}</div>
      <label className="news-ai-check"><input type="checkbox" checked={readOriginal} onChange={event => { setReadOriginal(event.target.checked); setEdited(true); }} disabled={!!pending || typeof apiClient.newsArticleReading !== "function"} /><span>自动补充重要原文（访问官方来源，附件另行确认）</span></label>
      {needsAI && !configured ? <p className="news-setup-required" role="status">此方式需要先保存一个主模型。<button type="button" className="news-inline-button" onClick={onAISetup}>先设置主模型</button>也可先选“只收集阅读”。</p> : needsAI ? <p>使用当前主模型：{routeLabel}。{mode === "manual" ? "监控开始后，选择一条消息并确认发送。" : "只解释相关的重要新事件和实质修订，重复、无关和历史回填不占自动额度。"}</p> : <p>此次仅开启收集与本机整理；已有模型设置保留，后续仍可手动解释。</p>}
      {mode === "automatic" && focus ? <><label>自动解释范围<select aria-label="自动解释范围" value={focus.automatic_scope} disabled={!!pending} onChange={event => { setEdited(true); setAcknowledged(false); setFocus(previous => ({ ...previous, automatic_scope: event.target.value })); }}><option value="market">一份综合解释：事实、影响条件与下一步</option><option value="facts">仅解释事实</option></select></label><label className="news-ai-check"><input type="checkbox" checked={acknowledged} onChange={event => setAcknowledged(event.target.checked)} disabled={!!pending || !configured} /><span>我确认按现有授权和共享预算自动解释重要变化，不重发结果未知的请求</span></label></> : null}
      {empty ? <p className="news-source-error">请至少选择一个关注对象。</p> : null}{error ? <p className="news-source-error" role="alert">{error}</p> : null}
      <div className="news-setup-actions"><button type="submit" className="news-button news-button-primary" disabled={disabled}>{pending === "setup" ? "正在逐项保存…" : overview?.enabled ? "保存工作方式并继续监控" : "3. 保存并开始监控"}</button><span>{edited ? "选择尚未保存。" : "只有点击按钮才会应用设置。"}成功步骤会保留，失败不会自动重试。</span></div>
      <p className="news-setup-results">3. 结果在哪里看：下方“值得关注”列表 → 打开消息 → 核对来源和时间 → 阅读解释与下一步。</p>
    </form>
  </details>;
}

export function NewsBacklogRecovery({ ai, busy, onPreview, onConfirm }) {
  const [limit, setLimit] = useState(3);
  const [cost, setCost] = useState("0.30");
  const [type, setType] = useState("market_impact");
  const [preview, setPreview] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const mounted = useRef(true); const request = useRef(null); const sequence = useRef(0); const inFlight = useRef(false);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; request.current?.abort(); }; }, []);
  useEffect(() => { request.current?.abort(); ++sequence.current; inFlight.current = false; setPreview(null); setLoading(false); }, [ai?.configured, ai?.provider, ai?.model, ai?.expires_at, ai?.automatic_since]);
  const invalidate = () => { request.current?.abort(); ++sequence.current; inFlight.current = false; setPreview(null); setLoading(false); setError(""); setNotice(""); };
  const execute = async confirm => {
    if (busy || loading || inFlight.current || !ai?.configured || (confirm && !preview?.token)) return;
    const controller = new AbortController(); request.current = controller; const current = ++sequence.current;
    inFlight.current = true; setLoading(true); setError(""); setNotice("");
    try {
      if (confirm) {
        const token = preview.token; setPreview(null);
        const result = await onConfirm({ token }, controller.signal);
        if (!controller.signal.aborted && mounted.current && current === sequence.current) {
          if (result.status !== "queued" || !Number.isInteger(result.count)) throw new Error("补处理状态尚未确认，请重新读取；不会自动重发。");
          setNotice(`已明确安排 ${result.count} 条旧待办；结果将在各条消息中显示。`);
        }
      } else {
        const result = await onPreview({ limit: Number(limit), max_cost_cny: Number(cost), task_type: type }, controller.signal);
        if (!controller.signal.aborted && mounted.current && current === sequence.current) {
          const validCount = Number.isInteger(result.count) && result.count >= 0 && result.count <= Math.min(Number(limit), 10);
          const validTasks = Array.isArray(result.tasks) && result.tasks.length === result.count;
          const validToken = result.count === 0 ? result.token === null : typeof result.token === "string" && result.token.trim().length > 0;
          if (!validCount || !validTasks || !validToken || result.confirmation_required !== true) throw new Error("补处理预览尚未确认，未发送请求。");
          setPreview(result);
        }
      }
    } catch (reason) { if (!controller.signal.aborted && mounted.current && current === sequence.current) setError(reason.message || "补处理状态尚未确认，不自动重试。"); }
    finally { if (current === sequence.current) { inFlight.current = false; if (mounted.current) setLoading(false); } }
  };
  return <details className="news-backlog-recovery"><summary>恢复旧待办 <span>先看数量和估算，再确认</span></summary><p>重启或重新授权不会自动补跑旧任务。这里仅安排预览列出的记录，已完成、失败和结果未知的请求继续保留。</p><div className="news-backlog-options"><label>最多处理条数<input type="number" min="1" max="10" value={limit} disabled={loading || busy} onChange={event => { invalidate(); setLimit(event.target.value); }} /></label><label>本批预约上限（元）<input type="number" min="0.01" max="3" step="0.01" value={cost} disabled={loading || busy} onChange={event => { invalidate(); setCost(event.target.value); }} /></label><label>解释类型<select value={type} disabled={loading || busy} onChange={event => { invalidate(); setType(event.target.value); }}><option value="market_impact">综合解释</option><option value="fact_reading">事实阅读</option></select></label></div>
    {!ai?.configured ? <p>先重新保存主模型设置，再查看本次可补处理范围；旧记录保持不变。</p> : <button type="button" className="news-button" disabled={busy || loading || !Number.isInteger(Number(limit)) || Number(limit) < 1 || Number(limit) > 10 || !Number.isFinite(Number(cost)) || Number(cost) <= 0 || Number(cost) > 3} onClick={() => void execute(false)}>{loading ? "正在处理…" : "预览可补处理范围（不调用 AI）"}</button>}
    {preview ? <div className="news-backlog-preview"><p>本次 {preview.count} 条 · 预约估算 {amount(preview.estimated_cost_cny)} 元 · 本批上限 {amount(preview.max_cost_cny)} 元 · 确认期限 {date(preview.expires_at)}</p><ul>{preview.tasks.map(task => <li key={task.task_id}>{task.title || "未命名消息"} · {task.task_type === "market_impact" ? "综合解释" : "事实阅读"} · {amount(task.estimated_cost_cny)} 元</li>)}</ul>{list(preview.reasons).length ? <ul>{preview.reasons.map((reason,index) => <li key={index}>{backlogReason(reason)}</li>)}</ul> : null}<p>使用当前主模型和原共享预算。预览不是实际扣费，确认后才安排执行。</p><button type="button" className="news-button news-button-primary" disabled={busy || loading || preview.count < 1} onClick={() => void execute(true)}>确认仅补处理这 {preview.count} 条</button></div> : null}
    {error ? <p className="news-source-error" role="alert">{error}</p> : null}{notice ? <p role="status">{notice}</p> : null}
  </details>;
}
