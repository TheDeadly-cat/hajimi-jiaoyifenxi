// Local presentation checks only. Reading this projection never fetches a source
// or creates an analysis; the backend owns selection and dependency verification.
const record = value => value && typeof value === "object" && !Array.isArray(value);
const text = value => typeof value === "string" ? value : "";
const uniqueStrings = values => Array.isArray(values) && values.every(value => typeof value === "string" && value) && new Set(values).size === values.length;
const safeUrl = value => { try { const url = new URL(value); return url.protocol === "https:" && !url.username && !url.password ? url.href : ""; } catch { return ""; } };
const sameValue = (left, right) => {
  if (left === right) return true;
  if (Array.isArray(left) || Array.isArray(right)) return Array.isArray(left) && Array.isArray(right) && left.length === right.length && left.every((value, index) => sameValue(value, right[index]));
  if (!record(left) || !record(right)) return false;
  const keys = Object.keys(left);
  return keys.length === Object.keys(right).length && keys.every(key => Object.hasOwn(right, key) && sameValue(left[key], right[key]));
};

function matchesSelectedProjection(selected, segment, groups) {
  // news_evidence_preview copies each selection row, adds the exact body slice
  // and source URL, and decodes compact cells. Check that whole projection,
  // including optional locator fields, rather than accepting equal IDs alone.
  if (Object.hasOwn(selected, "text") || selected.start !== 0 || selected.end !== Array.from(segment.text).length || selected.truncated !== false) return false;
  const projected = { ...selected, text: segment.text, url: selected.url || groups.find(group => group.document_id === selected.document_id)?.url || "" };
  if (Object.hasOwn(selected, "cells")) {
    if (selected.cell_encoding === "text_colspan_rowspan_tag_v1") {
      if (!Array.isArray(selected.cells) || selected.cells.some(cell => !Array.isArray(cell) || cell.length !== 4)) return false;
      projected.cells = selected.cells.map(([text, colspan, rowspan, tag]) => ({ text, colspan, rowspan, tag }));
    } else if (selected.cell_encoding) return false;
    delete projected.cell_encoding;
  }
  return sameValue(projected, segment);
}

export function validatedEvidencePreview(preview, { versionId, contentHash } = {}) {
  const invalid = reason => ({ valid: false, status: "unavailable", material: null, segments: [], package: null, reason });
  if (!record(preview)) return invalid("preview_missing");
  if (preview.schema !== "news_evidence_preview_v1" || preview.preview_only !== true || preview.model_called !== false) return invalid("preview_contract_mismatch");
  if (!text(versionId) || preview.version_id !== versionId) return invalid("preview_binding_mismatch");
  if (preview.status === "unavailable" && !contentHash && !preview.content_hash) return invalid(preview.reason || "content_hash_missing");
  if (!text(contentHash) || preview.content_hash !== contentHash) return invalid("preview_binding_mismatch");
  if (preview.status === "unavailable") return invalid(preview.reason || "preview_material_unavailable");
  if (!["ready", "no_selected_evidence"].includes(preview.status)) return invalid("preview_contract_mismatch");
  const material = preview.material, segments = preview.segments, pack = material?.evidence_package;
  if (!record(material) || !record(pack) || !record(material.source_text) || typeof material.source_text.body !== "string" || pack.version_id !== versionId || pack.content_hash !== contentHash || !Array.isArray(segments) || !Array.isArray(pack.groups) || !Array.isArray(pack.selection)) return invalid("preview_structure_mismatch");
  if (!uniqueStrings(segments.map(row => row?.id)) || !uniqueStrings(pack.selection.map(row => row?.id)) || !uniqueStrings(pack.groups.map(group => group?.id))) return invalid("preview_structure_mismatch");
  const rows = new Map(segments.map(row => [row.id, row]));
  if (pack.selection.length !== segments.length || pack.selection.some((row, index) => row.id !== segments[index].id)) return invalid("preview_structure_mismatch");
  if (segments.some(row => typeof row.text !== "string" || !text(row.document_id) || !text(row.source_segment_id) || !safeUrl(row.url))) return invalid("preview_structure_mismatch");
  if (pack.selection.some((selected, index) => !matchesSelectedProjection(selected, segments[index], pack.groups))) return invalid("preview_selection_mismatch");
  if (segments.some(row => row.kind === "table_row" && (!Array.isArray(row.cells) || !Number.isInteger(row.row_index) || row.row_index < 1 || !text(row.table_id) || row.cells.some(cell => !record(cell) || typeof cell.text !== "string" || !["th", "td"].includes(cell.tag) || !Number.isInteger(cell.colspan) || cell.colspan < 1 || !Number.isInteger(cell.rowspan) || cell.rowspan < 1) || row.text !== row.cells.map(cell => cell.text).join(" | ")))) return invalid("preview_table_structure_mismatch");
  const covered = new Set();
  for (const group of pack.groups) {
    if (!uniqueStrings(group.required_segment_ids) || !group.required_segment_ids.length || !safeUrl(group.url) || !text(group.document_id)) return invalid("preview_group_incomplete");
    const required = new Set(group.required_segment_ids);
    if (["targets", "dependencies"].some(key => group[key] !== undefined && (!uniqueStrings(group[key]) || group[key].some(id => !required.has(id))))) return invalid("preview_group_incomplete");
    if (group.profile_id || group.targets !== undefined || group.dependencies !== undefined) {
      if (!uniqueStrings(group.targets) || !group.targets.length || !uniqueStrings(group.dependencies)) return invalid("preview_group_incomplete");
      const described = new Set([...group.targets, ...group.dependencies]);
      if (described.size !== required.size || group.targets.some(id => group.dependencies.includes(id))) return invalid("preview_group_incomplete");
    }
    for (const id of required) {
      const row = rows.get(id);
      if (!row || row.document_id !== group.document_id || safeUrl(row.url) !== safeUrl(group.url)) return invalid("preview_group_incomplete");
      covered.add(id);
    }
  }
  if (covered.size !== segments.length || material.source_text.body !== segments.map(row => row.text).join("\n")) return invalid("preview_group_incomplete");
  if (preview.status === "ready" ? !segments.length || !pack.groups.length : segments.length || pack.groups.length) return invalid("preview_structure_mismatch");
  return { valid: true, status: preview.status, material, segments, package: pack, reason: preview.reason || null };
}

const groupNames = {
  gaap_net_income: "GAAP 净利润", gaap_diluted_eps: "GAAP 稀释每股收益", gaap_operating_cashflow: "季度 GAAP 经营现金流",
  cash_and_equivalents: "现金及等价物余额", total_assets: "总资产", closing_cash_including_restricted: "期末现金（含受限现金）",
  operating_cashflow_nine_months: "九个月经营现金净额", "unverified-remainder": "依赖尚未核对的其余材料",
};
export function evidenceGroupLabel(group) {
  const suffix = text(group?.id).split(":").at(-1);
  return groupNames[suffix] || (group?.kind === "table_context" ? "完整表格上下文" : "正文段落");
}
export function evidenceScopeLabel(scope) {
  return ({ quarterly_actual: "季度已报告数值", quarterly_actual_per_share: "季度每股数值", balance_as_of: "指定日期余额", nine_months_period_end_balance: "九个月期末余额", nine_months_actual: "九个月累计数值" })[scope] || "按下列原文核对期间与单位";
}
export function evidenceOmissionLabel(reason) {
  return ({ dependencies_unverified: "表头、期间、单位或脚注的依赖关系尚未核对", table_context_unverified: "表格结构或完整上下文尚未核对", segment_identity_unverified: "原段身份无法确认", budget: "完整组超出本次材料长度限制，整组省略", no_complete_groups_within_budget: "本次没有可完整选入的依据组", content_hash_missing: "旧记录缺少正文校验标识", content_hash_mismatch: "正文与保存的校验标识不一致", preview_binding_mismatch: "预览与当前查看版本的正文记录不一致", preview_missing: "此版本尚无可用的依据预览", source_not_readable: "此版本没有可读取的正文", version_required: "此版本身份尚未确认" })[reason] || "依据记录不完整或无法对应，暂不展示为可用依据";
}
export function attachmentReadingLabel(attachment) {
  const status = attachment?.status || attachment?.evidence?.content_status;
  if (attachment?.reason === "attachment_host_not_allowed") return "附件地址不在允许来源内，未读取";
  if (status === "partial_text") return attachment?.read === true ? "已取得部分文本，内容不完整" : "部分文本状态待核对，未确认为已读";
  if (status === "full_text" && attachment?.read === true) return "已取得附件文本";
  if (status === "fetch_failed") return "附件读取失败，未取得文本";
  if (status === "access_restricted") return "附件访问受限，未读取";
  if (status === "unsupported") return "附件格式暂不支持，未读取";
  return attachment?.read === true ? "已保存附件材料，完整性待确认" : "尚未读取";
}
export function attachmentReasonLabel(attachment) {
  const messages = { attachment_host_not_allowed: "附件链接超出允许访问的来源范围。", attachment_http_error: "来源未允许本次附件访问。", attachment_read_failed: "本次附件读取失败，正文已保存状态保持独立。", attachment_read_limit: "达到本次最多读取附件数量，尚未尝试。", source_wait_after_attachment_failure: "来源正在等待恢复，本次未继续读取此附件。" };
  const reason = messages[attachment?.reason] || (attachment?.reason || attachment?.error ? "附件读取记录仍有未解决的限制。" : "");
  const scope = attachment?.evidence?.reading_scope === "pdf_text_only_no_ocr_no_layout_validation" ? "仅提取 PDF 文本；未做图片文字识别或版式核验。" : "";
  return [reason, scope].filter(Boolean).join(" ");
}

export function resolveSavedEvidenceLocator(source, analysis, locator, quote) {
  const invalid = reason => ({ matched: false, reason });
  const versionId = analysis?.article_version_id || analysis?.candidate_result?.article_version_id;
  if (!text(versionId) || versionId !== source?.current_version_id || !record(locator) || !text(quote)) return invalid("version_or_locator_missing");
  const evidence = source?.evidence || source?.event?.current_version?.evidence || {};
  const segmentName = text(locator.segment_id), namespaced = /^attachment-([1-9]\d*):(.+)$/.exec(segmentName);
  const documentMatch = /^attachment-([1-9]\d*):/.exec(text(locator.document_id));
  if (namespaced && documentMatch && namespaced[1] !== documentMatch[1]) return invalid("document_mismatch");
  if (namespaced && locator.document_id && !documentMatch) return invalid("document_mismatch");
  const attachmentIndex = Number(namespaced?.[1] || documentMatch?.[1] || 0);
  const attachment = attachmentIndex ? evidence.attachments?.[attachmentIndex - 1] : null;
  if (attachmentIndex && (!attachment || attachment.read !== true || !["full_text", "partial_text"].includes(attachment.status || attachment.evidence?.content_status))) return invalid("attachment_unread");
  const document = attachmentIndex ? attachment.evidence : evidence;
  const segmentId = namespaced?.[2] || segmentName || text(locator.source_segment_id);
  if (locator.source_segment_id && locator.source_segment_id !== segmentId) return invalid("segment_mismatch");
  const documentUrl = safeUrl(attachmentIndex ? attachment.url : document?.url || source?.url);
  // Old article-only locators did not record a URL. New document-aware and
  // attachment pointers must identify the URL as well as the saved version.
  if ((locator.url && (!documentUrl || safeUrl(locator.url) !== documentUrl)) || ((attachmentIndex || locator.document_id || locator.source_segment_id) && !safeUrl(locator.url))) return invalid("url_mismatch");
  const segment = Array.isArray(document?.segments) ? document.segments.find(row => row.id === segmentId) : null;
  const characters = Array.from(text(segment?.text));
  if (!segment || !Number.isInteger(locator.start) || !Number.isInteger(locator.end) || locator.start < 0 || locator.end <= locator.start || locator.end > characters.length || characters.slice(locator.start, locator.end).join("") !== quote) return invalid("quote_mismatch");
  return { matched: true, segment, text: quote, url: documentUrl, sourceSegmentId: segmentId, label: attachmentIndex ? `已保存附件 ${attachmentIndex} · ${segmentId}` : `已保存正文 · ${segmentId}` };
}
