import { validatedEvidencePreview, evidenceOmissionLabel, attachmentReadingLabel } from "./newsEvidence.js";

function text(value) { return typeof value === "string" ? value.trim() : ""; }

function hasBodyEvidence(evidence) {
  return Boolean(evidence && (["full_text", "partial_text"].includes(evidence.content_status || evidence.status)
    || text(evidence.body_text) || evidence.segments?.length || evidence.tables?.length || evidence.paragraphs?.length
    || evidence.attachments?.some(attachment => attachment?.read === true)));
}

function bodyResearchEvidence(item, evidence) {
  const versionId = text(item.current_version_id);
  const contentHash = text(evidence?.content_hash);
  const current = item.event?.current_version;
  if (!versionId || current?.id && current.id !== versionId
    || current?.evidence?.content_hash && current.evidence.content_hash !== contentHash) {
    throw new Error("正文证据与当前消息版本不一致，未创建研究草稿。请重新读取消息详情。");
  }
  const preview = validatedEvidencePreview(item.evidence_preview, { versionId, contentHash });
  if (!preview.valid || !["ready", "no_selected_evidence"].includes(preview.status)) {
    throw new Error("当前正文没有可信的完整证据组预览，未创建研究草稿。请先取得与当前版本绑定的证据预览；不会改用原始正文、RSS 摘录或本机摘要。");
  }
  const packageHash = text(preview.package?.package_hash);
  if (!/^[0-9a-f]{64}$/.test(packageHash)) {
    throw new Error("完整证据组预览缺少可追溯的派生包摘要，未创建研究草稿。");
  }
  return { ...preview, versionId, contentHash, packageHash };
}

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
  const evidence = item.evidence || currentVersion?.evidence;
  const bodyEvidence = hasBodyEvidence(evidence);
  const preview = bodyEvidence ? bodyResearchEvidence(item, evidence) : null;
  const eventLines = [];
  if (preview) {
    const omitted = preview.package.omitted_group_reasons || {};
    const omissionLines = Object.entries(omitted).map(([reason, count]) => `${evidenceOmissionLabel(reason)}：${count} 组`);
    eventLines.push("", "已选完整事件证据（来源材料，仍需核验）",
      `事件编号：${text(item.event?.event_id) || "未知"}`,
      `内容版本：${text(currentVersion?.id || item.current_version_id) || "未知"}`,
      `原始内容摘要：${preview.contentHash}`,
      `派生证据包摘要：${preview.packageHash}`,
      `来源修订时间：${sourceTimestamp(currentVersion?.source_updated_at) || "未知"}`,
      `当前版本发现时间：${sourceTimestamp(currentVersion?.version_seen_at || item.version_seen_at) || "未知"}`,
      `实际读取状态：${text(evidence.content_status || evidence.status) || "未确认"}`,
      `抽取版本：${text(evidence.extractor_version || evidence.extraction_version) || "未标注"}`,
      `来源处数：${Array.isArray(item.event?.sources) ? item.event.sources.length : "未知"}；原始依据数：${Number.isInteger(item.event?.independent_evidence_count) ? item.event.independent_evidence_count : "未知"}`,
      "正文已读与附件已读分别记录；转载不增加独立依据。",
      preview.status === "no_selected_evidence" ? "本次没有完整证据组可以导入；草稿仅保留来源与省略范围，不包含正文、RSS 摘录或本机摘要。" : "下列组选段与后台派生证据包完全对应，依赖段和单元格结构一并保留；未按字符或行截断。",
      "", "证据组、依赖与省略记录", JSON.stringify(preview.package),
      "", "完整组选段与原始定位", ...preview.segments.map(segment => JSON.stringify(segment)),
      "", "未导入的范围", ...(omissionLines.length ? omissionLines : ["派生包没有记录被省略的证据组。"]),
      "未选中的正文、表格、RSS 摘录及本机摘要未附加到本草稿。",
      ...(evidence.truncated ? ["来源抽取记录包含截断；缺失部分仍未知。"] : []),
      "", "附件读取记录",
      ...(Array.isArray(evidence.attachments) && evidence.attachments.length ? evidence.attachments.map((a, index) => `附件 ${index + 1}：${sourceUrl(a.url) || "无可用链接"}；${attachmentReadingLabel(a)}；导入范围仅以以上已选证据组的文档身份为准。`) : [item.discovery_evidence?.attachment_references?.length ? "原文页面未取得可读附件；目录另列链接见下方，不能推定附件已读。" : "未发现或未取得附件清单，不能推定附件已读。"]),
      ...(Array.isArray(item.discovery_evidence?.attachment_references) && item.discovery_evidence.attachment_references.length ? [
        "目录另列的附件链接（发现时未读；当前已读取内容仅以正文附件记录与已选组为准）",
        ...item.discovery_evidence.attachment_references.map((a,index) => `目录链接 ${index+1}：${text(a.category) || "附件"}；${sourceUrl(a.url) || "无可用 HTTPS 链接"}；仅保存链接，未将附件内容加入草稿。`),
      ] : []),
      "", "市场解释边界", "预期与行情仅使用明确提供的资料；没有发布前预期不能判断超预期，没有带时点行情不能判断已定价。");
  }
  const knownSymbols = item.symbols || evidence?.symbols || item.event?.symbols || [];
  const symbols = Array.isArray(knownSymbols) ? [...new Set(knownSymbols.filter(value => typeof value === "string" && /^(US\.)?[A-Z][A-Z0-9.-]{0,14}$/.test(value)))] : [];
  const content = [
    "消息工作台研究摘录（未核验）",
    `${item.discovery_evidence?.title_basis === "directory_label" ? "目录条目名称（非原文标题）" : "原标题"}：${preview ? text(preview.material.source_text.title) : text(item.title)}`,
    `来源：${publisher}`,
    `原文链接：${url || "不可用"}`,
    clockAnomaly ? `来源标注时间（未确认）：${sourcePublishedAt || "未知"}` : `${revised ? "当前版本标注发布时间" : "原文发布时间"}：${publishedAt || "未知（来源未提供可靠时间）"}`,
    `本机发现时间：${discoveredAt || "未知"}`,
    `本机消息编号：${text(item.id)}`,
    preview ? "阅读边界：仅带入当前版本派生包已经选择的完整证据组及其依赖；未独立核验事实。生成草稿不保存资料、不启动模型，也不重新抓取原文。" : "阅读边界：以下仅为已保存的订阅摘录与本机整理，不是全文或事实核验；资料保存不会启动模型或重新抓取原文。",
    ...(!preview ? ["", "来源摘录", text(item.excerpt) || "来源未提供摘录。", "", "本机整理（规则生成，未核验）", text(item.summary) || "尚无本机整理。"] : []),
    ...(!preview && Array.isArray(item.material_quality?.warnings) ? ["", "本机材料检查（不代表事实核验）", ...item.material_quality.warnings.map(text).filter(Boolean)] : []),
    ...(!preview && Array.isArray(item.limitations) && item.limitations.some((value) => text(value)) ? ["", "原消息阅读提示", ...item.limitations.map(text).filter(Boolean)] : []),
    ...eventLines,
  ].join("\n");
  if (content.length > 50000) throw new Error(preview ? "完整证据组与定位记录超过资料长度上限（50000 字符），未创建研究草稿。不能截断证据组或改用未选原文，请选择更小的完整证据包。" : "这条消息的研究摘录超过资料长度上限，请先整理内容后手动添加。");
  return {
    title: text(item.title).slice(0, 120), kind: "note", source_url: url, content,
    metadata: { source_type: "internal_note", event_type: "other", publisher, published_at: publishedAt, symbols },
    _news_research: { handoff_id: text(handoffId), room_id: text(room.id), room_title: text(room.title), news_id: text(item.id), source_url: url, title_shortened: text(item.title).length > 120, event_id: text(item.event?.event_id), version_id: text(currentVersion?.id || item.current_version_id),
      ...(preview ? { evidence_package_hash: preview.packageHash, source_content_hash: preview.contentHash, evidence_preview_schema: "news_evidence_preview_v1" } : {}) },
  };
}

export function newsResearchSubmission(form, context, activeRoomId) {
  const handoff = form?._news_research;
  if (!handoff || !context || !handoff.handoff_id || handoff.handoff_id !== context.handoff_id
    || handoff.room_id !== context.room_id || handoff.news_id !== context.news_id
    || handoff.version_id !== context.version_id || handoff.evidence_package_hash !== context.evidence_package_hash
    || handoff.source_content_hash !== context.source_content_hash
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
