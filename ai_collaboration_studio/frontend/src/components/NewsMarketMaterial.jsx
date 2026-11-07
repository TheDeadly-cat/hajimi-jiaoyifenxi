import React, { useRef, useState } from "react";

export function marketSourceTime(local, zone) {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/.exec(local);
  if (!match || !["Asia/Shanghai", "America/New_York", "UTC"].includes(zone)) throw new Error("请填写来源日期、时间和时区。");
  const values = match.slice(1).map(value => Number(value || 0));
  const base = Date.UTC(values[0], values[1] - 1, ...values.slice(2));
  const formatter = new Intl.DateTimeFormat("sv-SE", { timeZone: zone, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" });
  const localParts = stamp => Object.fromEntries(formatter.formatToParts(new Date(stamp)).filter(part => part.type !== "literal").map(part => [part.type, Number(part.value)]));
  const observed = localParts(base);
  const offset = Date.UTC(observed.year, observed.month - 1, observed.day, observed.hour, observed.minute, observed.second) - base;
  const candidates = [...new Set([offset - 3600000, offset, offset + 3600000])].map(value => base - value).filter(stamp => {
    const part = localParts(stamp);
    return [part.year, part.month, part.day, part.hour, part.minute, part.second].every((value, index) => value === values[index]);
  });
  if (candidates.length !== 1) throw new Error("该本地时间不存在或处于夏令时回拨重叠时段，请核对来源，并改填明确的 UTC 时间。");
  return new Date(candidates[0]).toISOString();
}

export function marketMaterialPayload(form, item) {
  let url;
  try { url = new URL(form.sourceUrl); } catch { throw new Error("请填写可追溯的 HTTPS 来源链接。"); }
  if (url.protocol !== "https:" || url.username || url.password || (url.port && url.port !== "443")) throw new Error("请填写不含账户信息的标准 HTTPS 来源链接。");
  if (!form.value.trim() || !Number.isFinite(Number(form.value))) throw new Error("请填写明确的数值，未知时不要用 0 代替。");
  if (!form.period.trim() || !form.quote.trim()) throw new Error("请保留数值所属期间和完整的来源原句。");
  if (form.quote.trim().length > 2000) throw new Error("请将引用控制在 2000 字符内，并保留必要条件。");
  const quotedNumbers = (form.quote.match(/-?\d+(?:,\d{3})*(?:\.\d+)?/g) || []).map(value => Number(value.replaceAll(",", "")));
  if (!quotedNumbers.includes(Number(form.value))) throw new Error("填写的数值必须能在引用原句中找到；请核对数值与单位。");
  const sourceType = form.kind === "consensus" ? "analyst_research" : form.sourceType;
  const context = { kind: form.kind, symbol: form.symbol, metric: form.metric, period: form.period.trim(), unit: form.unit, currency: form.currency, comparison_basis: form.basis, value: Number(form.value), known_at: marketSourceTime(form.localTime, form.zone), quote: form.quote.trim() };
  if (form.kind === "reported_actual") { context.source_type = sourceType; context.event_url = item.url; }
  return { title: `${form.symbol} ${form.kind === "consensus" ? "共识预期" : form.kind === "old_guidance" ? "此前指引" : "公告实际值"} · ${form.period.trim()}`.slice(0, 120), kind: "note", source_url: url.href, content: form.quote.trim(), metadata: { source_type: sourceType, symbols: [form.symbol], event_type: form.kind === "old_guidance" ? "guidance" : "earnings", extraction_method: "manual_structured_market_context", market_context: context } };
}

export function NewsMarketMaterial({ item, roomId, apiClient, busy }) {
  const [form, setForm] = useState({ kind: "consensus", symbol: (item.symbols || item.evidence?.symbols || [])[0] || "US.MU", metric: "revenue", period: "", unit: "million", currency: "USD", basis: "absolute", value: "", localTime: "", zone: "Asia/Shanghai", sourceUrl: "", quote: "", sourceType: item.source_id?.startsWith("sec_") ? "regulatory_filing" : "company_ir" });
  const [ack, setAck] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const locked = useRef(false);
  const update = (key, value) => { setForm(previous => ({ ...previous, [key]: value })); setAck(false); setNotice(""); };
  if (!roomId || typeof apiClient.addMaterial !== "function") return null;
  const save = async event => {
    event.preventDefault(); if (locked.current || busy || !ack) return;
    let payload;
    try { payload = marketMaterialPayload(form, item); } catch (reason) { setError(reason.message); return; }
    locked.current = true; setSaving(true); setError(""); setNotice("");
    try { const result = await apiClient.addMaterial(roomId, payload); if (!result.material?.id) throw new Error("保存结果未确认，请先到所选房间查看已有资料，不要重复提交。"); setAck(false); setNotice("资料已保存到所选房间。需由你在房间中执行后续研究轮次，才能冻结进该轮次清单；本次没有启动模型或研究轮次。"); }
    catch (reason) {
      const descriptions = { market_material_quote_not_in_content: "引用原句必须完整保留在保存的资料正文中。", market_material_value_not_in_quote: "填写的数值必须出现在粘贴的来源原句中，请核对数值与单位。", market_material_known_at_requires_timezone: "请核对来源时间和时区，不能用无时区时间保存。", market_material_actual_source_not_official: "公告实际值需引用本标的已配置的官方来源链接。", market_material_metadata_fields_invalid: "结构化资料字段不完整，请检查材料类型及数值口径。" };
      const code = Object.keys(descriptions).find(value => String(reason.message || "").includes(value));
      setError(code ? descriptions[code] : "资料保存结果未确认，请先到所选房间查看已有资料；若未保存，请核对来源链接、时间与原句后再提交。");
    }
    finally { locked.current = false; setSaving(false); }
  };
  return <details className="news-market-material"><summary>补充预期、旧指引或公告数值</summary><p>手工保存到所选房间，未联网核验来源。实际录入时间会保留；今天补录的材料不能回填为公告发布前系统已知的预期。</p>
    <form onSubmit={event => void save(event)}><fieldset disabled={busy || saving}><legend>数值及比较口径</legend><div className="news-market-form-grid">
      <label>材料类型<select value={form.kind} onChange={e => update("kind", e.target.value)}><option value="consensus">发布前共识预期</option><option value="old_guidance">公司此前指引</option><option value="reported_actual">公告实际数值</option></select></label>
      <label>关联公司<select value={form.symbol} onChange={e => update("symbol", e.target.value)}>{["US.MU", "US.SNDK", "US.WDC", "US.STX"].map(symbol => <option key={symbol} value={symbol}>{symbol.slice(3)}</option>)}</select></label>
      <label>指标<select value={form.metric} onChange={e => update("metric", e.target.value)}><option value="revenue">收入</option><option value="eps">每股收益</option><option value="gross_margin">毛利率</option><option value="free_cash_flow">自由现金流</option></select></label>
      <label>所属期间<input value={form.period} maxLength={80} placeholder="例如 2026-Q3，以原文财政期间为准" onChange={e => update("period", e.target.value)} /></label>
      <label>数值<input value={form.value} inputMode="decimal" placeholder="未知时请留空" onChange={e => update("value", e.target.value)} /></label>
      <label>单位<select value={form.unit} onChange={e => update("unit", e.target.value)}><option value="million">百万</option><option value="billion">十亿</option><option value="per_share">每股</option><option value="percent">百分比</option><option value="units">个／单位</option></select></label>
      <label>币种<select value={form.currency} onChange={e => update("currency", e.target.value)}><option value="USD">美元</option><option value="CNY">人民币</option><option value="none">不适用</option></select></label>
      <label>比较口径<select value={form.basis} onChange={e => update("basis", e.target.value)}><option value="absolute">绝对值</option><option value="year_over_year">同比</option><option value="quarter_over_quarter">环比（季度）</option><option value="month_over_month">环比（月度）</option></select></label>
    </div></fieldset><fieldset disabled={busy || saving}><legend>来源与原句</legend>{form.kind !== "consensus" ? <label>官方材料类型<select value={form.sourceType} onChange={e => update("sourceType", e.target.value)}><option value="company_ir">公司官方公告</option><option value="regulatory_filing">监管申报</option></select></label> : null}
      <label>来源原文链接<input type="url" value={form.sourceUrl} maxLength={2000} onChange={e => update("sourceUrl", e.target.value)} /></label>
      <div className="news-market-form-grid"><label>来源标注日期与时间<input type="datetime-local" value={form.localTime} onChange={e => update("localTime", e.target.value)} /></label><label>原文时区<select value={form.zone} onChange={e => update("zone", e.target.value)}><option value="Asia/Shanghai">北京时间</option><option value="America/New_York">美国东部时间（自动处理夏令时）</option><option value="UTC">UTC</option></select></label></div>
      <label>包含数值、期间、单位与条件的来源原句<textarea rows={4} maxLength={2000} value={form.quote} onChange={e => update("quote", e.target.value)} /></label>
      {form.kind === "reported_actual" ? <p>实际值关联当前公告：{item.title}。将核对引用是否能在当前已保存的正文中找到。</p> : null}
      <label className="news-event-ack"><input type="checkbox" checked={ack} onChange={e => setAck(e.target.checked)} /><span>我已核对原句与填写的数值口径，并了解本次只保存资料。</span></label>
      <button type="submit" className="news-button" disabled={!ack || saving || busy}>{saving ? "正在保存资料…" : "保存为房间资料（不运行模型）"}</button>
    </fieldset></form>{error ? <p role="alert" className="news-source-error">{error}</p> : null}{notice ? <p role="status">{notice}</p> : null}</details>;
}
