function text(value) { return typeof value === "string" ? value.trim() : ""; }

function sourceTimestamp(value) {
  const candidate = text(value);
  const match = /^(\d{4})-(\d{2})-(\d{2})T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.exec(candidate);
  if (!match || !Number.isFinite(Date.parse(candidate))) return "";
  const day = new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3])));
  return day.getUTCFullYear() === Number(match[1]) && day.getUTCMonth() + 1 === Number(match[2]) && day.getUTCDate() === Number(match[3]) ? candidate : "";
}

function sourceUrl(value) {
  let url;
  try {
    url = new URL(text(value));
  } catch { return ""; }
  if (url.protocol !== "https:" || url.username || url.password) return "";
  if (url.href.length > 2000) throw new Error("原文链接超过 2000 字符，无法完整保存在资料链接字段。未创建草稿，请手动整理后添加资料；原消息仍保留。");
  return url.href;
}

export function buildNewsResearchDraft(item, room, handoffId) {
  if (!text(item?.id) || !text(item?.title)) throw new Error("消息身份不完整，请重新读取消息后再试。");
  if (!text(room?.id) || !text(room?.title) || !text(handoffId)) throw new Error("请明确选择要接收资料的讨论房间。");
  const sourcePublishedAt = sourceTimestamp(item.published_at);
  const clockAnomaly = item.material_quality?.freshness?.clock_anomaly === true;
  const publishedAt = clockAnomaly ? "" : sourcePublishedAt;
  const revised = item.material_quality?.freshness?.revision_after_discovery === true;
  const discoveredAt = sourceTimestamp(item.discovered_at);
  const url = sourceUrl(item.url);
  const publisher = text(item.source_name) || "来源名称未知";
  const currentVersion = item.event?.current_version;
  const evidence = currentVersion?.evidence || item.evidence;
  const eventLines = [];
  if (item.event && evidence) {
    const segments = Array.isArray(evidence.segments) ? evidence.segments : [...(evidence.paragraphs || []), ...(evidence.tables || [])];
    let remaining = 18000;
    const selected = [];
    for (const segment of segments) {
      const line = `[${text(segment.id) || "定位未标注"}] ${text(segment.text)}`;
      if (line.length > remaining) break;
      selected.push(line); remaining -= line.length;
    }
    const body = text(evidence.body_text);
    const fallback = !segments.length && body ? body.slice(0, 18000) : "";
    const omitted = selected.length < segments.length || (!segments.length && body.length > fallback.length);
    eventLines.push("", "已保存的事件证据（来源材料，仍需核验）",
      `事件编号：${text(item.event.event_id) || "未知"}`,
      `内容版本：${text(currentVersion?.id || item.current_version_id) || "未知"}`,
      `来源修订时间：${sourceTimestamp(currentVersion?.source_updated_at) || "未知"}`,
      `当前版本发现时间：${sourceTimestamp(currentVersion?.version_seen_at || item.version_seen_at) || "未知"}`,
      `实际读取状态：${text(evidence.content_status || evidence.status) || "未确认"}`,
      `抽取版本：${text(evidence.extractor_version) || "未标注"}`,
      `来源处数：${Array.isArray(item.event.sources) ? item.event.sources.length : "未知"}；原始依据数：${Number.isInteger(item.event.independent_evidence_count) ? item.event.independent_evidence_count : "未知"}`,
      "正文已读与附件已读分别记录；转载不增加独立依据。",
      ...selected, ...(fallback ? [fallback] : []),
      omitted ? "草稿长度限制：仅带入上述完整定位段落或前 18000 字符，未带入部分不作为本草稿证据。" : "已带入当前可用的定位内容。",
      ...(evidence.truncated ? ["来源抽取记录包含截断；缺失部分仍未知。"] : []),
      "", "附件读取记录",
      ...(Array.isArray(evidence.attachments) && evidence.attachments.length ? evidence.attachments.map((a, index) => `附件 ${index + 1}：${sourceUrl(a.url) || "无可用链接"}；${a.read === true ? "已读取" : "未读取"}；状态 ${text(a.status) || "未知"}`) : ["未发现或未取得附件清单，不能推定附件已读。"]),
      "", "市场解释边界", "预期与行情仅使用明确提供的资料；没有发布前预期不能判断超预期，没有带时点行情不能判断已定价。");
  }
  const knownSymbols = item.symbols || evidence?.symbols || item.event?.symbols || [];
  const symbols = Array.isArray(knownSymbols) ? [...new Set(knownSymbols.filter(value => typeof value === "string" && /^(US\.)?[A-Z][A-Z0-9.-]{0,14}$/.test(value)))] : [];
  const content = [
    "消息工作台研究摘录（未核验）",
    `原标题：${text(item.title)}`,
    `来源：${publisher}`,
    `原文链接：${url || "不可用"}`,
    clockAnomaly ? `来源标注时间（未确认）：${sourcePublishedAt || "未知"}` : `${revised ? "当前版本标注发布时间" : "原文发布时间"}：${publishedAt || "未知（来源未提供可靠时间）"}`,
    `本机发现时间：${discoveredAt || "未知"}`,
    `本机消息编号：${text(item.id)}`,
    evidence ? "阅读边界：以下包含当前版本已保存的摘录与证据，实际读取范围以各自记录为准，尚未独立核验事实；资料保存不会启动模型或重新抓取原文。" : "阅读边界：以下仅为已保存的订阅摘录与本机整理，不是全文或事实核验；资料保存不会启动模型或重新抓取原文。",
    "", "来源摘录", text(item.excerpt) || "来源未提供摘录。",
    "", "本机整理（规则生成，未核验）", text(item.summary) || "尚无本机整理。",
    ...(Array.isArray(item.material_quality?.warnings) ? ["", "本机材料检查（不代表事实核验）", ...item.material_quality.warnings.map(text).filter(Boolean)] : []),
    ...(Array.isArray(item.limitations) && item.limitations.some((value) => text(value)) ? ["", "原消息阅读提示", ...item.limitations.map(text).filter(Boolean)] : []),
    ...eventLines,
  ].join("\n");
  if (content.length > 50000) throw new Error("这条消息的研究摘录超过资料长度上限，请先整理内容后手动添加。");
  return {
    title: text(item.title).slice(0, 120), kind: "note", source_url: url, content,
    metadata: { source_type: "internal_note", event_type: "other", publisher, published_at: publishedAt, symbols },
    _news_research: { handoff_id: text(handoffId), room_id: text(room.id), room_title: text(room.title), news_id: text(item.id), source_url: url, title_shortened: text(item.title).length > 120, event_id: text(item.event?.event_id), version_id: text(currentVersion?.id || item.current_version_id) },
  };
}

export function newsResearchSubmission(form, context, activeRoomId) {
  const handoff = form?._news_research;
  if (!handoff || !context || !handoff.handoff_id || handoff.handoff_id !== context.handoff_id
    || handoff.room_id !== context.room_id || handoff.news_id !== context.news_id
    || activeRoomId !== context.room_id || form.id) {
    throw new Error("目标房间已改变或交接已取消，请返回消息工作台重新选择房间。");
  }
  if (!text(form.title) || text(form.title).length > 120 || !text(form.content) || text(form.content).length > 50000) throw new Error("请将资料标题控制在 120 字符内、内容控制在 50000 字符内，且两者均不能为空；尚未发送保存请求。");
  if (text(context.source_url).length > 2000) throw new Error("原文链接超过 2000 字符，尚未发送保存请求，不能截断后保存。");
  const { _news_research, ...payload } = form;
  return { roomId: context.room_id, payload: { ...payload, kind: "note", source_url: context.source_url, metadata: { ...payload.metadata, source_type: "internal_note" } } };
}

export function newsResearchStillCurrent(context, currentContext, activeRoomId) {
  return Boolean(context && context === currentContext && context.room_id === activeRoomId);
}
