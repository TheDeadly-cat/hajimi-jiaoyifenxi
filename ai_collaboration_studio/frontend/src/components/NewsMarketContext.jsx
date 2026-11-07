import React, { useEffect, useRef, useState } from "react";
import { eventTime, MarketImpact } from "./NewsEventCard";
import { NewsMarketMaterial } from "./NewsMarketMaterial";

export function NewsMarketContext({ item, rooms = [], apiClient, configured, routeLabel, busy }) {
  const [roomId, setRoomId] = useState("");
  const [snapshot, setSnapshot] = useState(null);
  const [roundId, setRoundId] = useState("");
  const [selected, setSelected] = useState([]);
  const [context, setContext] = useState(item.market_context || null);
  const [impact, setImpact] = useState(item.market_impact || null);
  const [pending, setPending] = useState("");
  const [error, setError] = useState("");
  const [confirm, setConfirm] = useState(false);
  const active = useRef(true);
  const request = useRef(null);
  const actionBusy = useRef(false);
  const binding = useRef(item.market_context?.context_id || null);
  useEffect(() => { active.current = true; return () => { active.current = false; request.current?.abort(); }; }, []);
  useEffect(() => {
    request.current?.abort(); actionBusy.current = false;
    binding.current = item.market_context?.context_id || null;
    setContext(item.market_context || null); setImpact(item.market_impact || null);
    setConfirm(false); setPending(""); setError(""); setRoomId(""); setSnapshot(null); setRoundId(""); setSelected([]);
  }, [item.id, item.current_version_id]);
  useEffect(() => {
    if (item.market_context?.context_id && (!binding.current || binding.current === item.market_context.context_id)) {
      binding.current = item.market_context.context_id; setContext(item.market_context);
      setImpact(item.market_impact || null);
    }
  }, [item.market_context, item.market_impact]);
  useEffect(() => {
    setSnapshot(null); setRoundId(""); setSelected([]); setError(""); setConfirm(false);
    if (!roomId || typeof apiClient.newsMarketOptions !== "function") return undefined;
    const controller = new AbortController(); setPending("room");
    apiClient.newsMarketOptions(roomId, controller.signal).then(value => {
      if (!controller.signal.aborted && active.current) setSnapshot(value);
    }).catch(reason => { if (!controller.signal.aborted && active.current) setError(reason.message || "房间资料暂时无法读取。"); }).finally(() => { if (!controller.signal.aborted && active.current) setPending(""); });
    return () => controller.abort();
  }, [roomId, apiClient]);
  useEffect(() => {
    if (!context?.context_id || !["queued", "pending", "running"].includes(impact?.status)) return undefined;
    const controller = new AbortController(); let reading = false;
    const contextId = context.context_id;
    const timer = window.setInterval(async () => {
      if (reading || document.visibilityState === "hidden") return; reading = true;
      try {
        const result = typeof apiClient.newsMarketSavedContext === "function"
          ? await apiClient.newsMarketSavedContext(item.id, contextId, controller.signal)
          : await apiClient.newsItem(item.id, controller.signal);
        const next = result.market_impact || result.item?.market_impact;
        if (!controller.signal.aborted && active.current && binding.current === contextId && next?.context_id === contextId) setImpact(next);
      } catch (reason) { if (!controller.signal.aborted && active.current) setError(reason.message || "市场分析状态未确认，不会重发。"); }
      finally { reading = false; }
    }, 2500);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [apiClient, item.id, context?.context_id, impact?.status]);
  if (typeof apiClient.newsMarketContext !== "function") return null;
  const availableRounds = Array.isArray(snapshot?.rounds) ? snapshot.rounds : [];
  const chosenRound = availableRounds.find(value => value.id === roundId);
  const materials = (Array.isArray(chosenRound?.materials) ? chosenRound.materials : []).filter(value => value.id && Number.isInteger(value.version) && value.version > 0);
  const newsOnly = context?.context_id && context.room_id == null && context.round_id == null;
  const materialNotSent = impact?.material_insufficient === true && impact.analysis_performed === false;
  const prepare = async (direct = false) => {
    if (actionBusy.current || busy || pending || (!direct && (!roomId || !roundId))) return;
    actionBusy.current = true; setPending(direct ? "direct" : "context"); setError(""); setConfirm(false);
    const controller = new AbortController(); request.current = controller;
    try {
      const payload = direct ? { room_id: null, round_id: null, material_refs: [] } : { room_id: roomId, round_id: roundId, material_refs: selected.map(id => ({ material_id: id, version: materials.find(value => value.id === id).version })) };
      const result = await apiClient.newsMarketContext(item.id, payload, controller.signal);
      if (!result.market_context?.context_id) throw new Error("资料准备结果尚未确认，请重新读取消息状态。");
      if (!controller.signal.aborted && active.current) {
        binding.current = result.market_context.context_id; setContext(result.market_context); setImpact(result.market_impact || null);
        if (direct && configured && !result.market_impact?.status) setConfirm(true);
      }
    } catch (reason) { if (!controller.signal.aborted && active.current) setError(reason.message || "材料准备尚未完成。"); }
    finally { if (request.current === controller) { actionBusy.current = false; if (active.current) setPending(""); } }
  };
  const analyze = async () => {
    if (actionBusy.current || busy || !configured || !context?.context_id || !confirm || typeof apiClient.newsMarketAnalyze !== "function") return;
    actionBusy.current = true; setPending("analysis"); setError(""); setConfirm(false);
    const controller = new AbortController(); request.current = controller;
    try {
      const result = await apiClient.newsMarketAnalyze(item.id, context.context_id, controller.signal);
      if (!(result.market_impact || result.result)?.status) throw new Error("市场分析请求结果未确认，请重新读取消息，勿重复发送。");
      if (!controller.signal.aborted && active.current) setImpact(result.market_impact || result.result);
    } catch (reason) { if (!controller.signal.aborted && active.current) setError(reason.message || "分析结果未确认，不会自动重发。"); }
    finally { if (request.current === controller) { actionBusy.current = false; if (active.current) setPending(""); } }
  };
  return <section className="news-market-context" aria-label="解释这条消息与下一步">
    <h4>解释这条消息与下一步</h4>
    <p>使用这条消息已有的原文或摘录，解释事实、变化、影响条件，以及下一步核实什么。无需先创建房间或研究轮次。</p>
    <button className="news-button" type="button" disabled={!!pending || busy} onClick={() => void prepare(true)}>{pending === "direct" ? "正在准备已有证据…" : "解释这条消息"}</button>
    <p className="news-event-caption">先核对发送范围，再确认调用{routeLabel || "当前主模型"}。缺少预期时不能判断是否超预期；缺少行情时不能断言市场已经怎样反应。</p>
    {context?.context_id ? <div className="news-market-bound">
      <p><strong>本次范围：{newsOnly ? "仅这条消息的已有证据" : "这条消息与所选已保存资料"}</strong>。资料已绑定到保存版本，尚未发送的请求仍需确认。</p>
      {Array.isArray(context.missing) && context.missing.length ? <ul>{context.missing.map((line, index) => <li key={index}>{line}</li>)}</ul> : null}
      <p className="news-event-caption">行情时点：{eventTime(context.market?.captured_at)} · 已绑定发布前预期资料：{context.consensus?.length || 0} 份</p>
      {materialNotSent ? <div className="news-market-material-insufficient" role="status"><strong>材料不足，本次未发送模型请求</strong><p>{typeof impact.error === "string" ? impact.error : "缺少可引用的正文或原句，只有标题或行情不足以分析新闻影响。"}</p><p>核对来源、补充可引用原文后再准备材料。已有记录保留。</p></div> : impact && !["completed", "success", "queued", "pending", "running", "preview", "context_ready"].includes(impact.status) ? <p role="status">{impact.status === "unknown" ? "本次市场分析结果未确认，保留记录，不自动重发。" : impact.status === "needs_review" ? "市场分析需要复核，暂不作为已确认判断。" : "本次市场分析未完成，记录已保留。"}</p> : null}
      {["queued", "pending", "running"].includes(impact?.status) ? <p role="status">市场分析已排队或处理中，按已确认预算运行。</p> : null}
      {(!impact?.status || ["preview", "context_ready"].includes(impact.status)) ? !configured ? <p>需要模型解释时，请先在上方保存 AI 设置，再回来确认发送。</p> : confirm ? <div role="group" aria-label="确认市场影响分析"><p>向{routeLabel || "当前主模型"}发送{newsOnly ? "这条消息的已有证据" : "当前版本证据、已绑定的预期与行情"}。本次只安排一次分析，计入现有共享预算。</p><button className="news-button" type="button" disabled={!!pending || busy} onClick={() => void analyze()}>确认分析市场影响</button><button className="news-inline-button" type="button" onClick={() => setConfirm(false)}>暂不发送市场分析</button></div> : <button className="news-button" type="button" disabled={!!pending || busy} onClick={() => setConfirm(true)}>核对市场分析发送范围</button> : null}
    </div> : null}
    {error ? <p className="news-source-error" role="alert">{error}</p> : null}
    {impact && ["completed", "success", "preview", "context_ready"].includes(impact.status) ? <MarketImpact impact={impact} /> : null}
    {typeof apiClient.newsMarketOptions === "function" ? <details className="news-market-deeper"><summary>结合预期和行情深入分析</summary>
      <p>需要判断是否超预期或核对市场反应时，再选择已有资料。绑定只读取本机记录；发布时间之后才取得的预期不会冒充发布前共识。</p>
      <label>选择资料所在房间<select value={roomId} disabled={!!pending || busy} onChange={e => setRoomId(e.target.value)}><option value="">请选择房间</option>{rooms.map(room => <option key={room.id} value={room.id}>{room.title}</option>)}</select></label>
      <NewsMarketMaterial key={roomId} item={item} roomId={roomId} apiClient={apiClient} busy={busy || !!pending} />
      {pending === "room" ? <p role="status">正在读取已保存资料…</p> : snapshot ? <>
        <label>已有行情轮次<select value={roundId} disabled={!!pending || busy} onChange={e => { setRoundId(e.target.value); setSelected([]); setConfirm(false); }}><option value="">请选择已保存轮次</option>{availableRounds.map(round => <option key={round.id} value={round.id}>保存轮次 · {eventTime(typeof round.created_at === "number" ? new Date(round.created_at).toISOString() : round.created_at)}</option>)}</select></label>
        {!availableRounds.length ? <p className="news-event-caption">此房间尚无已保存轮次；你仍可使用上方“解释这条消息”。</p> : null}
        <fieldset disabled={!!pending || busy}><legend>选择用于比较的已有资料</legend>{materials.length ? materials.map(material => <label key={material.id}><input type="checkbox" checked={selected.includes(material.id)} onChange={e => { setSelected(previous => e.target.checked ? [...previous, material.id] : previous.filter(id => id !== material.id)); setConfirm(false); }} />{material.title || "未命名资料"} · 版本 {material.version}</label>) : <p>没有可绑定的已保存资料。可先核对新闻本身，需要深入比较时再补充有来源和时间的资料。</p>}</fieldset>
        <button className="news-button" type="button" disabled={!!pending || busy || !roundId} onClick={() => void prepare(false)}>{pending === "context" ? "正在绑定资料…" : "绑定所选资料（不调用 AI）"}</button>
      </> : null}
    </details> : null}
  </section>;
}
