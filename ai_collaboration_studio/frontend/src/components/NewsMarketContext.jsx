import React, { useEffect, useRef, useState } from "react";
import { eventTime, MarketImpact } from "./NewsEventCard";
import { NewsMarketMaterial } from "./NewsMarketMaterial";

export function NewsMarketContext({ item, rooms, apiClient, configured, routeLabel, busy }) {
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
  useEffect(() => { active.current = true; return () => { active.current = false; request.current?.abort(); }; }, []);
  useEffect(() => { setContext(item.market_context || null); setImpact(item.market_impact || null); setConfirm(false); }, [item.current_version_id, item.market_context, item.market_impact]);
  useEffect(() => {
    request.current?.abort(); setSnapshot(null); setRoundId(""); setSelected([]); setError(""); setConfirm(false);
    if (!roomId) return undefined;
    const controller = new AbortController(); request.current = controller; setPending("room");
    apiClient.newsMarketOptions(roomId, controller.signal).then(value => {
      if (!controller.signal.aborted && active.current) setSnapshot(value);
    }).catch(reason => { if (!controller.signal.aborted && active.current) setError(reason.message || "房间资料暂时无法读取。"); }).finally(() => { if (!controller.signal.aborted && active.current) setPending(""); });
    return () => controller.abort();
  }, [roomId, apiClient]);
  useEffect(() => {
    if (!["queued", "pending", "running"].includes(impact?.status)) return undefined;
    const controller = new AbortController(); let reading = false;
    const timer = window.setInterval(async () => {
      if (reading || document.visibilityState === "hidden") return; reading = true;
      try { const result = await apiClient.newsItem(item.id, controller.signal); if (!controller.signal.aborted && active.current && result.item?.market_impact) setImpact(result.item.market_impact); }
      catch (reason) { if (!controller.signal.aborted && active.current) setError(reason.message || "市场分析状态未确认，不会重发。"); }
      finally { reading = false; }
    }, 2500);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [apiClient, item.id, impact?.status]);
  if (typeof apiClient.newsMarketContext !== "function" || typeof apiClient.newsMarketOptions !== "function") return null;
  const availableRounds = Array.isArray(snapshot?.rounds) ? snapshot.rounds : [];
  const latest = availableRounds[0];
  const chosenRound = availableRounds.find(value => value.id === roundId);
  const materials = (Array.isArray(chosenRound?.materials) ? chosenRound.materials : []).filter(value => value.id && Number.isInteger(value.version) && value.version > 0);
  const prepare = async () => {
    if (actionBusy.current || busy || !roomId || !roundId) return;
    actionBusy.current = true; setPending("context"); setError(""); setConfirm(false);
    const controller = new AbortController(); request.current = controller;
    try {
      const result = await apiClient.newsMarketContext(item.id, { room_id: roomId, round_id: roundId || null, material_refs: selected.map(id => ({ material_id: id, version: materials.find(value => value.id === id).version })) }, controller.signal);
      if (!result.market_context?.context_id) throw new Error("资料绑定结果尚未确认，请重新读取消息状态。");
      if (!controller.signal.aborted && active.current) { setContext(result.market_context); setImpact(result.market_impact || null); }
    } catch (reason) { if (!controller.signal.aborted && active.current) setError(reason.message || "材料绑定尚未完成。"); }
    finally { actionBusy.current = false; if (active.current) setPending(""); }
  };
  const analyze = async () => {
    if (actionBusy.current || busy || !configured || !context?.context_id || !confirm) return;
    actionBusy.current = true; setPending("analysis"); setError(""); setConfirm(false);
    const controller = new AbortController(); request.current = controller;
    try {
      const result = await apiClient.newsMarketAnalyze(item.id, context.context_id, controller.signal);
      if (!(result.market_impact || result.result)?.status) throw new Error("市场分析请求结果未确认，请重新读取消息，勿重复发送。");
      if (!controller.signal.aborted && active.current) setImpact(result.market_impact || result.result);
    } catch (reason) { if (!controller.signal.aborted && active.current) setError(reason.message || "分析结果未确认，不会自动重发。"); }
    finally { actionBusy.current = false; if (active.current) setPending(""); }
  };
  return <details className="news-market-context"><summary>补充已有资料，分析市场影响</summary>
    <p>从讨论房间选择已经保存的材料和行情轮次。绑定只读取本机记录；发布时间之后才取得的预期不会冒充发布前共识。</p>
    <label>选择资料所在房间<select value={roomId} disabled={!!pending || busy} onChange={e => setRoomId(e.target.value)}><option value="">请选择房间</option>{rooms.map(room => <option key={room.id} value={room.id}>{room.title}</option>)}</select></label>
    <NewsMarketMaterial key={roomId} item={item} roomId={roomId} apiClient={apiClient} busy={busy || !!pending} />
    {pending === "room" ? <p role="status">正在读取已保存资料…</p> : snapshot ? <>
      <label>已有行情轮次<select value={roundId} disabled={!!pending || busy} onChange={e => { setRoundId(e.target.value); setSelected([]); setConfirm(false); }}><option value="">请选择已保存轮次</option>{latest?.id ? <option value={latest.id}>最近保存轮次 · {eventTime(typeof latest.created_at === "number" ? new Date(latest.created_at).toISOString() : latest.created_at)}</option> : null}</select></label>
      {!latest?.id ? <p className="news-event-caption">此房间没有可选择的已保存轮次；不会为此抓取新行情。</p> : null}
      <fieldset disabled={!!pending || busy}><legend>选择用于比较的已有资料</legend>{materials.length ? materials.map(material => <label key={material.id}><input type="checkbox" checked={selected.includes(material.id)} onChange={e => { setSelected(previous => e.target.checked ? [...previous, material.id] : previous.filter(id => id !== material.id)); setConfirm(false); }} />{material.title || "未命名资料"} · 版本 {material.version}</label>) : <p>没有可绑定的已保存资料。可先在讨论房间保存有来源和时间的预期或旧指引。</p>}</fieldset>
      <button className="news-button" type="button" disabled={!!pending || busy || !roundId} onClick={() => void prepare()}>{pending === "context" ? "正在绑定资料…" : "绑定所选资料（不调用 AI）"}</button>
    </> : null}
    {context?.context_id ? <div className="news-market-bound"><p>材料已绑定到这条消息的证据版本。缺失或过期内容仍按未知处理。</p>{Array.isArray(context.missing) && context.missing.length ? <ul>{context.missing.map((line,index) => <li key={index}>{line}</li>)}</ul> : null}<p className="news-event-caption">行情时点：{eventTime(context.market?.captured_at)} · 已绑定发布前预期资料：{context.consensus?.length || 0} 份</p>{impact && !["completed", "success", "queued", "pending", "running"].includes(impact.status) ? <p role="status">{impact.status === "unknown" ? "本次市场分析结果未确认，保留记录，不自动重发。" : impact.status === "needs_review" ? "市场分析需要复核，暂不作为已确认判断。" : impact.status === "failed" ? "本次市场分析未完成，记录已保留。" : "这是已绑定材料的预览，尚未调用市场分析模型。"}</p> : null}
      {["queued", "pending", "running"].includes(impact?.status) ? <p role="status">市场分析已排队或处理中，按已确认预算运行。</p> : null}
      {(!impact?.status || ["preview", "context_ready"].includes(impact.status)) ? !configured ? <p>需要模型解释时，请先在上方保存 AI 设置，再回来确认发送。</p> : confirm ? <div role="group" aria-label="确认市场影响分析"><p>向{routeLabel || "当前所选模型"}发送当前版本证据、已绑定的预期与行情。安排一次独立市场分析，计入现有共享预约预算；预约不等于供应商账单。</p><button className="news-button" type="button" disabled={!!pending || busy} onClick={() => void analyze()}>确认分析市场影响</button><button className="news-inline-button" type="button" onClick={() => setConfirm(false)}>暂不发送市场分析</button></div> : <button className="news-button" type="button" disabled={!!pending || busy} onClick={() => setConfirm(true)}>核对市场分析发送范围</button> : null}
    </div> : null}
    {error ? <p className="news-source-error" role="alert">{error}</p> : null}
    {impact && ["completed", "success", "preview", "context_ready"].includes(impact.status) ? <MarketImpact impact={impact} /> : null}
  </details>;
}
