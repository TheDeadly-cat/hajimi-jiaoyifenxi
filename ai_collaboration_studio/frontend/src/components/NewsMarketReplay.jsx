import React, { useEffect, useRef, useState } from "react";
import { contentStateLabels, eventLink, eventTime, MarketImpact } from "./NewsEventCard";
import { marketSourceTime } from "./NewsMarketMaterial";

const PAGE = 50;
const runningStates = ["queued", "pending", "running"];
const timeQualified = value => typeof value === "string" && /(?:Z|[+-]\d{2}:\d{2})$/.test(value) && Number.isFinite(Date.parse(value));
const preciseTime = value => BigInt(Date.parse(value)) * 1000000n + BigInt((/\.(\d+)(?:Z|[+-]\d{2}:\d{2})$/.exec(value)?.[1] || "").slice(3).padEnd(6, "0").slice(0, 6));
const sameTime = (left, right) => timeQualified(left) && timeQualified(right) && preciseTime(left) === preciseTime(right);
const plainValue = value => ["string", "number"].includes(typeof value) ? String(value) : "未知";
const roundTime = value => eventTime(typeof value === "number" ? new Date(value).toISOString() : value);
const materialKey = value => `${value.id}:${value.version}`;
function replayError(reason, fallback) {
  const labels = { news_version_not_known_at_as_of: "截止时间早于所选新闻版本被系统发现的时刻，请调整时间或选择更早版本。", news_version_not_latest_at_as_of: "截止时刻已有较新版本，请选择当时最新的版本或调整截止时间。", news_publication_future_at_as_of: "截止时刻尚不能确认这条新闻已发布。", market_as_of_requires_timezone: "请填写明确的回放截止时间和时区。", market_as_of_in_future: "回放截止时间不能晚于现在。", market_checkpoint_not_known_at_as_of: "所选轮次在截止时刻尚未保存，请重新选择。", market_material_not_known_at_as_of: "所选资料版本在截止时刻尚未保存，请重新选择。", market_snapshot_not_known_at_as_of: "所选行情在截止时刻尚不可得，请重新选择。" };
  return labels[reason?.code] || labels[reason?.message] || reason?.message || fallback;
}
function boundContext(value, versionId, expected = {}) {
  if (!value?.context_id || value.mode !== "historical_replay" || value.article_version_id !== versionId || !timeQualified(value.as_of)) return false;
  if (expected.contextId && value.context_id !== expected.contextId) return false;
  if (expected.asOf && !sameTime(value.as_of, expected.asOf)) return false;
  if (Object.hasOwn(expected, "roomId") && (value.room_id || null) !== expected.roomId) return false;
  if (Object.hasOwn(expected, "roundId") && (value.round_id || null) !== expected.roundId) return false;
  if (expected.materialRefs) {
    if (!Array.isArray(value.material_refs) || value.material_refs.some(ref => typeof ref?.material_id !== "string" || !Number.isInteger(ref.version) || ref.version < 1)) return false;
    const identity = refs => JSON.stringify(refs.map(ref => [ref.material_id, ref.version]).sort((left, right) => left[0].localeCompare(right[0]) || left[1] - right[1]));
    if (identity(value.material_refs) !== identity(expected.materialRefs)) return false;
  }
  return true;
}

export function NewsMarketReplay({ item, version, rooms = [], apiClient, configured, routeLabel, busy, initiallyOpen = false }) {
  const [localTime, setLocalTime] = useState("");
  const [zone, setZone] = useState("Asia/Shanghai");
  const [exactTime, setExactTime] = useState("");
  const [roomId, setRoomId] = useState("");
  const [roundId, setRoundId] = useState("");
  const [options, setOptions] = useState(null);
  const [selected, setSelected] = useState([]);
  const [saved, setSaved] = useState(null);
  const [context, setContext] = useState(null);
  const [impact, setImpact] = useState(null);
  const [pending, setPending] = useState("");
  const [error, setError] = useState("");
  const [confirm, setConfirm] = useState(false);
  const active = useRef(true), request = useRef(null), sequence = useRef(0), actionBusy = useRef(false);
  useEffect(() => { active.current = true; return () => { active.current = false; request.current?.abort(); sequence.current++; }; }, []);
  let asOf = "", timeError = "";
  try {
    if (exactTime) asOf = exactTime;
    else if (localTime) asOf = marketSourceTime(localTime, zone);
    if (asOf && Date.parse(asOf) > Date.now()) timeError = "回放截止时间不能晚于现在。";
    if (asOf && (!timeQualified(version?.version_seen_at) || preciseTime(asOf) < preciseTime(version.version_seen_at))) timeError = "截止时间早于所选版本被系统发现的时刻，或该版本的发现时间未确认。";
  } catch (reason) { timeError = replayError(reason, "请核对回放时间和时区。"); }
  const rounds = Array.isArray(options?.rounds) ? options.rounds : [];
  const chosenRound = rounds.find(round => round.id === roundId);
  const materials = (Array.isArray(chosenRound?.materials) ? chosenRound.materials : []).filter(material => typeof material.id === "string" && Number.isInteger(material.version) && material.version > 0);
  const available = typeof apiClient.newsMarketContext === "function";
  const canPrepare = available && asOf && !timeError && (!roomId || (options?.asOf === asOf && roundId));
  const start = kind => { request.current?.abort(); const controller = new AbortController(); request.current = controller; const token = ++sequence.current; actionBusy.current = true; setPending(kind); setError(""); return { controller, token }; };
  const current = action => active.current && !action.controller.signal.aborted && action.token === sequence.current;
  const finish = action => { if (current(action)) { actionBusy.current = false; setPending(""); } };
  const invalidate = () => { request.current?.abort(); sequence.current++; actionBusy.current = false; setPending(""); setOptions(null); setRoundId(""); setSelected([]); setContext(null); setImpact(null); setConfirm(false); setError(""); };

  const loadOptions = async (append = false) => {
    if (busy || actionBusy.current || !roomId || !asOf || timeError || typeof apiClient.newsMarketOptions !== "function") return;
    if (!append) { setRoundId(""); setSelected([]); setContext(null); setImpact(null); setConfirm(false); }
    const action = start("options"), requestedAsOf = asOf;
    try {
      const offset = append ? options.nextOffset : 0;
      const value = await apiClient.newsMarketOptions(roomId, action.controller.signal, { as_of: requestedAsOf, limit: PAGE, offset });
      if (!Array.isArray(value.rounds) || !Number.isInteger(value.total) || value.total < 0 || !sameTime(value.as_of, requestedAsOf) || value.scope !== "saved_rounds_as_of" || typeof value.has_more !== "boolean" || (value.has_more && (!Number.isInteger(value.next_offset) || value.next_offset <= offset))) throw new Error("未能确认这些轮次属于所选回放时刻，请重新读取。");
      if (value.rounds.some(round => !round.id || !timeQualified(round.known_at) || preciseTime(round.known_at) > preciseTime(requestedAsOf) || !Number.isFinite(typeof round.created_at === "number" ? round.created_at : Date.parse(round.created_at)) || (typeof round.created_at === "number" ? round.created_at : Date.parse(round.created_at)) > Date.parse(requestedAsOf))) throw new Error("返回的轮次包含截止时刻之后或时间未确认的记录，暂不供选择。");
      if (current(action)) { setOptions({ asOf: requestedAsOf, total: value.total, nextOffset: value.next_offset, hasMore: value.has_more, skipped: (append ? options.skipped : 0) + (Number.isInteger(value.skipped_unverified) ? value.skipped_unverified : 0), rounds: append ? [...rounds, ...value.rounds.filter(row => !rounds.some(prior => prior.id === row.id))] : value.rounds }); if (!append) { setRoundId(""); setSelected([]); } }
    } catch (reason) { if (current(action)) setError(replayError(reason, "历史轮次暂时无法读取。")); } finally { finish(action); }
  };
  const prepare = async () => {
    if (busy || actionBusy.current || !canPrepare) return;
    const payload = { room_id: roomId || null, round_id: roomId ? roundId : null, material_refs: materials.filter(material => selected.includes(materialKey(material))).map(material => ({ material_id: material.id, version: material.version })), version_id: version.id, as_of: asOf };
    const action = start("prepare"); setConfirm(false);
    try {
      const result = await apiClient.newsMarketContext(item.id, payload, action.controller.signal);
      if (!boundContext(result.market_context, version.id, { asOf: payload.as_of, roomId: payload.room_id, roundId: payload.round_id, materialRefs: payload.material_refs }) || (result.market_impact && result.market_impact.context_id !== result.market_context.context_id)) throw new Error("返回材料与所选新闻版本、截止时间、房间或资料版本不一致，暂不显示为回放依据。");
      if (current(action)) { setContext(result.market_context); setSaved(previous => previous?.contexts.some(value => value.context_id === result.market_context.context_id) ? previous : null); if (result.market_impact) setImpact(result.market_impact); else if (context?.context_id !== result.market_context.context_id) setImpact(null); }
    } catch (reason) { if (current(action)) setError(replayError(reason, "历史材料准备尚未确认，可查看此版本的已保存回放记录。")); } finally { finish(action); }
  };
  const loadSaved = async (append = false) => {
    if (busy || actionBusy.current || typeof apiClient.newsMarketReplays !== "function") return;
    const action = start("saved");
    try {
      const previous = append && saved ? saved.contexts : [];
      const result = await apiClient.newsMarketReplays(item.id, version.id, action.controller.signal, { limit: PAGE, offset: previous.length });
      if (!Array.isArray(result.contexts) || !Number.isInteger(result.total) || result.contexts.some(value => !value.context_id || value.article_version_id !== version.id || !timeQualified(value.as_of))) throw new Error("已保存回放的版本身份未能确认。");
      if (current(action)) setSaved({ total: result.total, contexts: [...previous, ...result.contexts.filter(value => !previous.some(prior => prior.context_id === value.context_id))] });
    } catch (reason) { if (current(action)) setError(replayError(reason, "已保存回放暂时无法读取。")); } finally { finish(action); }
  };
  const openSaved = async contextId => {
    if (busy || actionBusy.current || typeof apiClient.newsMarketSavedContext !== "function") return;
    const action = start("open"); setConfirm(false); setContext(null); setImpact(null);
    try {
      const result = await apiClient.newsMarketSavedContext(item.id, contextId, action.controller.signal);
      if (!boundContext(result.market_context, version.id, { contextId }) || (result.market_impact && result.market_impact.context_id !== contextId)) throw new Error("保存材料不属于所选历史版本，未使用当前版本代替。");
      if (current(action)) { const value = result.market_context; setContext(value); setImpact(result.market_impact || null); setExactTime(value.as_of); setLocalTime(""); setZone("UTC"); setRoomId(value.room_id || ""); setRoundId(value.round_id || ""); setOptions(null); setSelected([]); }
    } catch (reason) { if (current(action)) setError(replayError(reason, "这份历史回放暂时无法打开。")); } finally { finish(action); }
  };
  const analyze = async () => {
    if (busy || actionBusy.current || !confirm || !configured || context?.analysis_allowed !== true || typeof apiClient.newsMarketAnalyze !== "function") return;
    const action = start("analysis"), contextId = context.context_id; setConfirm(false);
    try {
      const result = await apiClient.newsMarketAnalyze(item.id, contextId, action.controller.signal, { acknowledge_historical: true });
      const value = result.market_impact || result.result;
      if (!value?.status || value.context_id !== contextId) throw new Error("历史分析请求结果未确认，保留本次记录，勿重复发送。");
      if (current(action)) setImpact(value);
    } catch (reason) { if (current(action)) { setError(replayError(reason, "历史分析结果未确认，不会自动重发。")); setImpact({ status: "unknown", context_id: contextId }); } } finally { finish(action); }
  };
  useEffect(() => {
    if (!context?.context_id || !runningStates.includes(impact?.status) || typeof apiClient.newsMarketSavedContext !== "function") return undefined;
    const controller = new AbortController(); let reading = false;
    const timer = window.setInterval(async () => {
      if (reading || document.visibilityState === "hidden") return; reading = true;
      try { const result = await apiClient.newsMarketSavedContext(item.id, context.context_id, controller.signal); if (!controller.signal.aborted && active.current) { if (!boundContext(result.market_context, version.id, { contextId: context.context_id, asOf: context.as_of }) || (result.market_impact && result.market_impact.context_id !== context.context_id)) throw new Error("历史分析的回放身份不一致，保留此前状态。"); setImpact(result.market_impact || impact); } }
      catch (reason) { if (!controller.signal.aborted && active.current) setError(replayError(reason, "历史分析状态暂未确认，不会重发。")); } finally { reading = false; }
    }, 2500);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [apiClient, item.id, version.id, context?.context_id, context?.as_of, impact?.status]);

  const roomName = id => rooms.find(room => room.id === id)?.title || (id ? "已保存的资料房间" : "仅所选新闻版本");
  const evidence = Array.isArray(context?.evidence) ? context.evidence.filter(value => value.type !== "market_snapshot") : [];
  const quoteRows = Array.isArray(context?.market?.rows) ? context.market.rows : [];
  const materialNotSent = impact?.material_insufficient === true && impact.analysis_performed === false;
  return <details className="news-market-replay" aria-label="历史市场材料回放" open={initiallyOpen || undefined}>
    <summary>历史时点回放：准备与查看已有材料</summary><p>当前选择版本 {version.version_number}，首次发现于 {eventTime(version.version_seen_at)}（北京时间）。截止时刻必须已知此版本，且尚无较新版本。</p>
    <p>准备只保存本机已有新闻、资料版本与行情的不可变上下文；不会抓取来源或调用模型。缺失材料保持未知。</p>
    <div className="news-market-form-grid"><label>回放截止日期与时间<input aria-label="回放截止日期与时间" type="datetime-local" step="1" value={localTime} disabled={busy} onChange={event => { invalidate(); setExactTime(""); setLocalTime(event.target.value); }} /></label><label>回放时区<select aria-label="回放时区" value={zone} disabled={busy} onChange={event => { invalidate(); setExactTime(""); setZone(event.target.value); }}><option value="Asia/Shanghai">北京时间（UTC+08:00）</option><option value="America/New_York">美国东部时间（含夏令时）</option><option value="UTC">UTC（协调世界时）</option></select></label></div>
    <button className="news-inline-button" type="button" disabled={busy || !timeQualified(version.version_seen_at)} onClick={() => { invalidate(); setLocalTime(""); setZone("UTC"); setExactTime(version.version_seen_at); }}>采用此版本首次发现时刻</button>
    {asOf ? <p className="news-replay-cutoff">选定截止时刻：{eventTime(asOf)}（北京时间）{exactTime ? <small> · 使用保存记录的精确时刻：{exactTime}</small> : null}</p> : <p>请明确选择回放截止时间；不会默认采用当前时间。</p>}{timeError ? <p className="news-source-error" role="alert">{timeError}</p> : null}
    <label>资料范围<select aria-label="回放资料房间" value={roomId} disabled={busy} onChange={event => { invalidate(); setRoomId(event.target.value); }}><option value="">仅准备所选新闻版本</option>{rooms.map(room => <option key={room.id} value={room.id}>{room.title}</option>)}</select></label>
    {roomId ? <><button className="news-button" type="button" disabled={!!pending || busy || !asOf || !!timeError} onClick={() => void loadOptions()}>读取截止时刻前的已存轮次</button>{options ? <><label>选择当时已保存的轮次<select aria-label="回放研究轮次" value={roundId} disabled={!!pending || busy} onChange={event => { setRoundId(event.target.value); setSelected([]); setContext(null); setImpact(null); setConfirm(false); }}><option value="">请选择轮次，不默认采用最新</option>{rounds.map(round => <option key={round.id} value={round.id}>资料已知于 {eventTime(round.known_at)} · 轮次 {roundTime(round.created_at)}</option>)}</select></label>{!rounds.length ? <p>{options.hasMore ? "本页没有合格轮次，可继续加载后续记录。" : "截止时刻前没有可用的已保存轮次。可改为仅准备新闻版本，行情与预期保持缺失。"}</p> : null}{options.hasMore ? <button className="news-inline-button" type="button" disabled={!!pending || busy} onClick={() => void loadOptions(true)}>加载更多当时已存轮次</button> : null}{options.skipped > 0 ? <p>已略过 {options.skipped} 个无法核对的候选记录；数量不代表合格轮次总数。</p> : null}<fieldset disabled={!!pending || busy}><legend>选择该轮次中的资料版本（最多 12 份）</legend>{materials.length ? materials.map(material => <label key={materialKey(material)}><input type="checkbox" checked={selected.includes(materialKey(material))} disabled={!selected.includes(materialKey(material)) && selected.length >= 12} onChange={event => { setSelected(prior => event.target.checked ? [...prior, materialKey(material)] : prior.filter(value => value !== materialKey(material))); setContext(null); setImpact(null); setConfirm(false); }} />{material.title || "未命名资料"} · 版本 {material.version}</label>) : <p>未选轮次或没有可绑定的资料版本。</p>}</fieldset></> : null}</> : <p>将保留新闻版本与缺失项，不补入其他房间或最新行情。</p>}
    <button className="news-button" type="button" disabled={!!pending || busy || !canPrepare} onClick={() => void prepare()}>{pending === "prepare" ? "正在保存历史材料…" : "准备历史材料（不调用 AI）"}</button>
    <div className="news-replay-saved"><button className="news-inline-button" type="button" disabled={!!pending || busy || typeof apiClient.newsMarketReplays !== "function"} onClick={() => void loadSaved()}>查看此版本已保存的回放</button>{saved ? <>{saved.contexts.length ? <ul>{saved.contexts.map(value => <li key={value.context_id}><button className="news-button" type="button" aria-pressed={context?.context_id === value.context_id} disabled={!!pending || busy} onClick={() => void openSaved(value.context_id)}>打开截止于 {eventTime(value.as_of)} 的回放 · {roomName(value.room_id)}</button></li>)}</ul> : <p>此版本尚无已保存的回放材料。</p>}{saved.contexts.length < saved.total ? <button className="news-inline-button" type="button" disabled={!!pending || busy} onClick={() => void loadSaved(true)}>加载更多已保存回放</button> : null}</> : null}</div>
    {pending && pending !== "prepare" ? <p role="status">{pending === "analysis" ? "正在安排已确认的历史分析…" : "正在读取本机保存记录…"}</p> : null}
    {context ? <div className="news-market-bound news-replay-bound"><h4>已保存的历史回放材料</h4><p>新闻版本 {context.news?.version_number ?? version.version_number} · {contentStateLabels[context.news?.content_status] || "阅读范围未确认"} · 截止 {eventTime(context.as_of)}（北京时间）</p><p>资料范围：{roomName(context.room_id)}{context.round_id ? " · 明确绑定的已存轮次" : ""}。这份记录不会随当前版本更新。</p><p>行情时点：{eventTime(context.market?.captured_at)} · {context.market?.status === "available" ? "已保存行情可用于该截止时点" : "行情缺失、过期或尚未核验"}</p>{context.missing?.length ? <ul>{context.missing.map((line,index) => <li key={index}>{line}</li>)}</ul> : null}
      <details className="news-replay-materials"><summary>查看已绑定的资料原句与时点</summary>{evidence.length ? evidence.map((entry,index) => <article key={entry.id || index}><strong>{({ consensus: "共识预期", old_guidance: "此前指引", reported_actual: "已报告实际值", deterministic_comparison: "同口径数值比较" })[entry.type] || "保存依据"} · {plainValue(entry.symbol)} {plainValue(entry.period)}</strong><blockquote>{typeof entry.text === "string" ? entry.text : "原句未保留"}</blockquote><p>来源时点：{eventTime(entry.asof)} · 系统已知：{eventTime(entry.known_at)}</p>{eventLink(entry.url) ? <a href={eventLink(entry.url)} target="_blank" rel="noopener noreferrer">查看保存的来源链接</a> : null}</article>) : <p>没有可引用的已绑定市场资料，不能补写预期。</p>}</details>
      {quoteRows.length ? <details className="news-replay-quotes"><summary>查看当时保存的行情</summary>{quoteRows.map((row,index) => <article className="news-replay-quote-record" key={row.symbol || index}><h5>{plainValue(row.symbol)}</h5><dl><div><dt>价格</dt><dd><span className="news-replay-number">{plainValue(row.last)}</span>{typeof row.currency === "string" ? ` ${row.currency}` : "（币种未记录）"}</dd></div><div><dt>成交量</dt><dd className="news-replay-number">{plainValue(row.volume)}</dd></div><div className="news-replay-quote-time"><dt>报价时点（北京时间）</dt><dd>{timeQualified(row.updated_at) ? eventTime(row.updated_at) : "报价时点未确认"}</dd><dt>来源原记录</dt><dd>{plainValue(row.market_time)}{timeQualified(row.market_time) ? "" : "（时区未注明）"}</dd></div></dl></article>)}</details> : null}
      {materialNotSent ? <div className="news-market-material-insufficient" role="status"><strong>材料不足，本次未发送模型请求</strong><p>{typeof impact.error === "string" ? impact.error : "缺少可引用的正文或原句，只有标题或行情不足以分析新闻影响。"}</p><p>已保存材料与记录保留；可选择当时有可引用证据的版本和截止时间重新准备。</p></div> : runningStates.includes(impact?.status) ? <p role="status">历史市场分析已排队或处理中，仅查看这份回放的状态。</p> : ["unknown", "needs_review", "failed"].includes(impact?.status) ? <p role="status">{impact.status === "unknown" ? "历史分析结果未确认" : impact.status === "needs_review" ? "历史分析需要复核" : "历史分析未完成"}；记录保留，不会自动重发。</p> : null}
      {!impact?.status || ["preview", "context_ready"].includes(impact.status) ? context.analysis_allowed !== true ? <p>这份回放尚不具备分析条件，材料仍可查看。</p> : !configured ? <p>材料已保存，尚未调用模型。需要分析时，请先在上方配置 AI，再单独确认发送。</p> : confirm ? <div className="news-replay-confirm" role="group" aria-label="确认历史市场分析"><p>将向{routeLabel || "当前所选模型"}发送新闻版本 {context.news?.version_number ?? version.version_number}、截止 {eventTime(context.as_of)}（北京时间）的已保存资料与行情；资料范围为{roomName(context.room_id)}{context.round_id ? "中的已绑定轮次" : ""}。这是一次独立历史分析，计入原有共享预约预算；预约不等于供应商账单。</p><button className="news-button" type="button" disabled={!!pending || busy} onClick={() => void analyze()}>确认分析这个历史时点</button><button className="news-inline-button" type="button" onClick={() => setConfirm(false)}>暂不发送历史分析</button></div> : <button className="news-button" type="button" disabled={!!pending || busy || typeof apiClient.newsMarketAnalyze !== "function"} onClick={() => setConfirm(true)}>核对历史市场分析发送范围</button> : null}
      {["completed", "success"].includes(impact?.status) ? <MarketImpact impact={impact} /> : null}
    </div> : null}
    {error ? <p className="news-source-error" role="alert">{error}</p> : null}
  </details>;
}
