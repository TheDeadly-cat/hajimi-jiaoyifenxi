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
  const content = [
    "消息工作台研究摘录（未核验）",
    `原标题：${text(item.title)}`,
    `来源：${publisher}`,
    `原文链接：${url || "不可用"}`,
    clockAnomaly ? `来源标注时间（未确认）：${sourcePublishedAt || "未知"}` : `${revised ? "当前版本标注发布时间" : "原文发布时间"}：${publishedAt || "未知（来源未提供可靠时间）"}`,
    `本机发现时间：${discoveredAt || "未知"}`,
    `本机消息编号：${text(item.id)}`,
    "阅读边界：以下仅为已保存的订阅摘录与本机整理，不是全文或事实核验；资料保存不会启动模型或重新抓取原文。",
    "", "来源摘录", text(item.excerpt) || "来源未提供摘录。",
    "", "本机整理（规则生成，未核验）", text(item.summary) || "尚无本机整理。",
    ...(Array.isArray(item.material_quality?.warnings) ? ["", "本机材料检查（不代表事实核验）", ...item.material_quality.warnings.map(text).filter(Boolean)] : []),
    ...(Array.isArray(item.limitations) && item.limitations.some((value) => text(value)) ? ["", "原消息阅读提示", ...item.limitations.map(text).filter(Boolean)] : []),
  ].join("\n");
  if (content.length > 50000) throw new Error("这条消息的研究摘录超过资料长度上限，请先整理内容后手动添加。");
  return {
    title: text(item.title).slice(0, 120), kind: "note", source_url: url, content,
    metadata: { source_type: "internal_note", event_type: "other", publisher, published_at: publishedAt, symbols: [] },
    _news_research: { handoff_id: text(handoffId), room_id: text(room.id), room_title: text(room.title), news_id: text(item.id), source_url: url, title_shortened: text(item.title).length > 120 },
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
