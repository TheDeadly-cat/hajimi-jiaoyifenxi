import React from "react";
import "./NewsContentQuality.css";
import { evidenceGroupLabel, evidenceScopeLabel, evidenceOmissionLabel, resolveSavedEvidenceLocator } from "../newsEvidence";

export function EvidencePackageScope({ evidencePackage, segments }) {
  if (!evidencePackage || typeof evidencePackage !== "object") return null;
  const groups = Array.isArray(evidencePackage.groups) ? evidencePackage.groups : [];
  const rows = new Map((Array.isArray(segments) ? segments : []).map(row => [row.id, row]));
  const profiles = Array.isArray(evidencePackage.verified_profiles) ? evidencePackage.verified_profiles : [];
  const omissions = evidencePackage.omitted_group_reasons && typeof evidencePackage.omitted_group_reasons === "object" ? Object.entries(evidencePackage.omitted_group_reasons) : [];
  const showRows = ids => <ol className="news-evidence-group-rows">{(ids || []).map(id => {
    const row = rows.get(id);
    return <li key={id}><small>{row?.kind === "table_row" ? `表格第 ${row.row_index} 行` : "原段"} · {row?.source_segment_id || id}</small>{row ? <p>{row.text}</p> : <p className="news-event-caption">此记录保留了定位标识，未附原段文本；请核对保存的引文。</p>}</li>;
  })}</ol>;
  return <div className="news-evidence-scope">
    <p>{Array.isArray(evidencePackage.groups) ? `已选 ${groups.length} 组` : "此记录未保存完整组清单"} · 省略{Number.isInteger(evidencePackage.omitted_group_count) ? ` ${evidencePackage.omitted_group_count} 组` : "数量未记录"}。{groups.length ? "表头、期间、单位与脚注随所属组保留；" : ""}省略内容不能用于补足结论。</p>
    {groups.map(group => <details className="news-evidence-group" key={group.id}>
      <summary>{evidenceGroupLabel(group)}<span>{evidenceScopeLabel(group.scope)} · {(group.required_segment_ids || []).length} 处原段</span></summary>
      {sourceHref(group.url) ? <a href={sourceHref(group.url)} target="_blank" rel="noopener noreferrer">查看本组来源原文</a> : null}
      <h5>核对目标</h5>{showRows(group.targets?.length ? group.targets : group.required_segment_ids)}
      {group.dependencies?.length ? <><h5>期间、单位与脚注依据</h5>{showRows(group.dependencies)}</> : <p className="news-event-caption">期间和单位按以上完整原文核对；这里不推断缺失口径。</p>}
    </details>)}
    {profiles.length ? <div className="news-evidence-profile"><p>配套关系来自对已保存材料的离线核对；仅覆盖所选已报告项目，其他明细、调整项和预测仍需另查。</p>{profiles.map(profile => <p key={profile.id}>原文币种符号：{plain(profile.currency_symbol) || "未标注"}；明确币种：{plain(profile.currency_iso) || "未确认，不能仅凭符号推定"}。</p>)}</div> : null}
    {omissions.length ? <div className="news-evidence-omissions"><strong>省略原因</strong><ul>{omissions.map(([reason, count]) => <li key={reason}>{evidenceOmissionLabel(reason)}（{count} 组）</li>)}</ul></div> : null}
    {evidencePackage.excerpt_omitted_for_table_context ? <p>订阅摘录也已省略，避免将缺少配套说明的财务数字作为依据。</p> : null}
    {evidencePackage.truncated ? <p className="news-event-warning">材料或选择范围存在截断、省略；这里只说明已保留范围。</p> : null}
  </div>;
}

export function MaterialQuality({ quality }) {
  if (!quality) return null;
  const warnings = Array.isArray(quality.warnings) ? quality.warnings : [];
  return <section className="news-material-quality" aria-label="阅读前核对">
    <h3>阅读前核对 <span>本机检查 · 不消耗模型额度</span></h3>
    <p className={quality.status === "clock_anomaly" || quality.can_analyze === false ? "news-source-error" : ""}>{quality.label || (quality.scope?.kind === "article_evidence" ? quality.scope?.full_text_read ? "已保存正文证据，附件范围另列" : "已保存部分正文证据，需核对缺失范围" : "仅有订阅标题与摘录")}</p>
    {warnings.length ? <ul>{warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul> : null}
    {quality.can_analyze === false ? <p>请先打开官方原文核对；当前材料不会发送给模型。</p> : null}
  </section>;
}

export function AnalysisEvidence({ analysis }) {
  const evidence = Array.isArray(analysis?.evidence) ? analysis.evidence : [];
  if (analysis?.legacy) return <p className="news-analysis-legacy">这是保留的旧版分析，尚未经过新规则检查。你仍可阅读原结果；切换模型或重新打开页面不会自动重新分析或扣费。</p>;
  if (!evidence.length && !analysis?.evidence_package && !analysis?.revision_comparison) return null;
  return <><RevisionComparisonScope comparison={analysis?.revision_comparison} />{analysis?.evidence_package ? <details className="news-analysis-used-scope"><summary>已使用范围（保存的分析记录）</summary><p>这是此分析记录保存的材料范围。当前可用预览不会补入这份旧记录；范围记录不代表分析结论已经核实。</p><EvidencePackageScope evidencePackage={analysis.evidence_package} /></details> : null}{evidence.length ? <details className="news-analysis-evidence">
    <summary>查看 AI 说法与引用原文（{evidence.length}）</summary>
    <p>引文与已保存材料对应，仅说明出处；数字含义、条件与结论仍需人工核对。{analysis?.scope?.kind === "article_evidence" ? "本次使用已保存的正文证据包，具体阅读范围和截断以保存记录为准；附件另行标记。" : "全文尚未读取。"}</p>
    <ol>{evidence.map((entry, index) => <li key={index}>
      <strong>{plain(entry?.claim) || "这条要点的文字未保留"}</strong>
      <small>{entry?.evidence_field === "revision" ? `版本对照：${entry?.locator?.revision_role === "before" ? "旧版本原文" : entry?.locator?.revision_role === "after" ? "所选新版本原文" : "具体版本未定位"}${entry?.locator?.source_segment_id ? ` · ${entry.locator.source_segment_id}` : ""}` : entry?.evidence_field === "title" ? "记录引用的来源标题" : entry?.evidence_field === "excerpt" ? "记录引用的来源摘录" : entry?.evidence_field === "body" ? `记录引用的${/^attachment-\d+:/.test(entry?.locator?.segment_id || entry?.locator?.document_id || "") ? "附件" : "正文"}${entry?.locator?.segment_id ? ` · ${entry.locator.segment_id}` : "（具体段落未标注）"}` : "引用位置未确认"}</small>
      <blockquote>{plain(entry?.quote) || "没有可显示的引用原文"}</blockquote>
      {sourceHref(entry?.locator?.url) ? <a href={sourceHref(entry.locator.url)} target="_blank" rel="noopener noreferrer">查看该引文记录的来源</a> : null}
    </li>)}</ol>
  </details> : null}</>;
}

export function RevisionComparisonScope({ comparison }) {
  if (!comparison || typeof comparison !== "object") return null;
  if (comparison.status === "source_text_unchanged") return <p className="news-revision-comparison">已核对两版保存的正文或摘录及其结构，未发现变化；其他来源信息仍需分别核对。</p>;
  const documents = Array.isArray(comparison.documents) ? comparison.documents : [];
  const available = comparison.status === "available" && documents.length === 2 && documents.some(row => row.role === "before") && documents.some(row => row.role === "after");
  return <section className="news-revision-comparison" aria-label="本次版本对照范围"><h4>本次版本对照范围</h4>
    {available ? <><p>已同时纳入旧版与新版完整的保存材料；只比较以下范围，附件内容不在本次对照中。</p><ul>{documents.map(row => <li key={row.role}>{row.role === "before" ? "旧版本" : "所选新版本"} {Number.isInteger(row.version_number) ? row.version_number : "编号未记录"} · {row.scope === "original_excerpt_only_full_body_unknown" ? "原始摘录，全文未知" : "保存正文，完整表格上下文随原段保留"}{sourceHref(row.url) ? <> · <a href={sourceHref(row.url)} target="_blank" rel="noopener noreferrer">来源链接</a></> : null}</li>)}</ul><p>旧版数值只作为旧版依据，不代表当前结果；对照不等于独立核实。</p></> : <p>没有完整、可比的前后材料进入本次分析，新增或更正尚无法核验。{comparison.reason === "comparison_budget" ? "前后材料超出预算，已整组省略。" : "旧版可能缺失、读取范围不同或尚无法确认。"}</p>}
  </section>;
}

// Presentation only: resolve the backend's saved pointers. Do not infer a
// disputed sentence, revalidate a result, or parse raw JSON into trusted facts.
const plain = (value) => typeof value === "string" ? value : "";
const object = (value) => value && typeof value === "object" && !Array.isArray(value) ? value : null;
const indexed = (value, pattern) => {
  const match = typeof value === "string" && value.match(pattern);
  const index = match ? Number(match[1]) : -1;
  return Number.isSafeInteger(index) ? index : -1;
};
const claimIndex = (field) => indexed(field, /^key_points\.(0|[1-9]\d*)\.claim$/);
const quoteIndex = (field) => indexed(field, /^key_points\.(0|[1-9]\d*)\.quote$/);
const uncertaintyIndex = (field) => indexed(field, /^uncertainties\.(0|[1-9]\d*)$/);
const listAt = (values, index) => Array.isArray(values) && index >= 0 && index < values.length ? values[index] : null;

export function analysisStateLabel(analysis) {
  const labels = { completed: "AI 解释已生成，供理解参考", success: "AI 解释已生成，供理解参考",
    needs_review: "内容待复核，不会自动重试", unknown: "本次结果未确认，不会自动重试",
    queued: "已加入分析队列", pending: "正在分析", running: "正在分析",
    failed: "本次分析未完成", material_insufficient: "材料不足，未发送模型请求",
  };
  const status = String(analysis?.status || "").toLowerCase();
  return Object.hasOwn(labels, status) ? labels[status] : "分析状态待确认";
}

function issueLabel(issue) {
  const labels = {
    statistical_comparison_basis: "来源没有充分支持这项同比或环比口径。",
    uncertainty_denies_supported_basis: "不确定项把来源已经明确说明的比较口径说成未知。",
    assertion_uncertainty_conflict: "正文与不确定项对同一信息的说法不一致。",
    statistical_value_unit: "数字或单位与引用内容不符，或尚无法对应。",
    monetary_value_currency: "金额、币种或数量级与来源不符，或尚无法对应。",
    monetary_period: "金额所属期间与来源不符，或尚无法对应。",
    statistical_period: "数据所属期间与来源不符，或尚无法对应。",
    enforcement_action_count: "来源没有充分支持这项行动数量。",
    explicit_relationship_direction_obscured: "原文明确的行动关系被写成未知，需要核对。",
    analysis_unsupported_relationship_direction: "原文的模糊关系被写成了明确方向，需要核对。",
    publication_check_direction: "发布时间与审核检查时间的先后关系表述不符。",
    publication_age_claim: "发布时间距离本次检查的描述需要核对。",
    discovery_recency_claim: "首次发现时间与“新发现”的表述不符。",
    publication_not_confirmed: "来源时间存在异常，不能确认已经发布。",
    validation_unconfirmed: "内容检查未能确认完成，不能当作已经通过。",
  };
  const label = plain(issue?.message) || (Object.hasOwn(labels, issue?.code) ? labels[issue.code] : "这项表述与来源或时间信息无法充分对应，需进一步核对。");
  return `${issue?.status === "contradicted" ? "存在冲突：" : issue?.status === "unknown" ? "依据不足：" : ""}${label}`;
}

function candidateText(candidate, field) {
  if (field === "summary") return { label: "AI 写的摘要（整段）", text: plain(candidate?.summary), whole: true };
  const point = claimIndex(field);
  if (point >= 0) {
    const evidence = listAt(candidate?.evidence, point);
    const claim = plain(listAt(candidate?.key_points, point));
    // The candidate has a flattened list and its original evidence list. If
    // they disagree, selecting either one would manufacture a correspondence.
    return { label: `AI 写的第 ${point + 1} 条要点`, text: claim && claim === plain(evidence?.claim) ? claim : "" };
  }
  const uncertainty = uncertaintyIndex(field);
  if (uncertainty >= 0) return { label: `AI 列出的第 ${uncertainty + 1} 条不确定项`, text: plain(listAt(candidate?.uncertainties, uncertainty)) };
  return { label: "涉及的模型表述", text: "" };
}

function sourceHref(value) {
  try {
    const url = new URL(value);
    return url.protocol === "https:" && !url.username && !url.password ? url.href : "";
  } catch { return ""; }
}

const formatter = new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
function savedTime(value) {
  const date = typeof value === "string" && value ? new Date(value) : null;
  return date && Number.isFinite(date.getTime()) ? formatter.format(date) : "未确认";
}

function EvidenceReference({ reference, field, candidate, analysis, source }) {
  const index = quoteIndex(reference);
  if (index >= 0) {
    const entry = listAt(candidate?.evidence, index);
    const ownIndex = claimIndex(field);
    const quote = plain(entry?.quote);
    const sourceField = entry?.evidence_field;
    const claim = plain(listAt(candidate?.key_points, index));
    const locator = object(entry?.locator);
    const located = sourceField === "body" ? resolveSavedEvidenceLocator(source, analysis, locator, quote) : null;
    const currentSource = ["title", "excerpt"].includes(sourceField) ? plain(source?.[sourceField]) : located?.matched ? located.text : "";
    if ((ownIndex >= 0 && ownIndex !== index) || !quote || !claim || claim !== plain(entry?.claim) || !currentSource.includes(quote)) {
      return <p className="news-review-unmapped">记录中的引文无法与当前保存的来源准确对应，暂不并排当作依据；可展开技术详情核对原记录。</p>;
    }
    return <div className="news-review-support"><h5>记录引用的原句 · {sourceField === "title" ? "来源标题" : sourceField === "body" ? located.label : "来源摘录"}</h5><blockquote>{quote}</blockquote>{located?.url ? <a href={located.url} target="_blank" rel="noopener noreferrer">查看原句所属来源</a> : null}<p>这段引文可在当前保存的材料中找到；它不代表 AI 对原文的解读正确。</p></div>;
  }
  if (reference === "source_text.title" || reference === "source_text.excerpt") {
    const sourceField = reference === "source_text.title" ? "title" : "excerpt";
    const text = plain(source?.[sourceField]);
    return text ? <div className="news-review-support"><h5>{sourceField === "title" ? "已保存来源标题" : "已保存来源摘录"}（整段）</h5><blockquote>{text}</blockquote><p>记录只指向这一类来源材料，没有定位具体原句；这里展示当前保存的内容。</p></div> : <p className="news-review-unmapped">当前没有可显示的来源{sourceField === "title" ? "标题" : "摘录"}，无法确认对应原句。</p>;
  }
  if (reference === "uncertainties") {
    const notes = Array.isArray(candidate?.uncertainties) ? candidate.uncertainties.filter((entry) => plain(entry)) : [];
    return notes.length ? <div className="news-review-support"><h5>记录关联的不确定项</h5><p>记录未指出是哪一项与上面的表述冲突，因此保留全部内容供核对。</p><ul>{notes.map((note, index) => <li key={index}>{note}</li>)}</ul></div> : <p className="news-review-unmapped">相关不确定项未保留，无法确认具体对应关系。</p>;
  }
  const freshness = object(analysis?.freshness) || object(candidate?.freshness);
  const timeFields = { "metadata.published_at": ["保存的来源标注时间", freshness?.published_at],
    "metadata.discovered_at": ["保存的首次发现时间", freshness?.discovered_at],
    checked_at: ["保存的审核检查时间", analysis?.quality_checked_at || candidate?.quality_checked_at] };
  if (Object.hasOwn(timeFields, reference)) {
    const [label, value] = timeFields[reference];
    return <p className="news-review-time-reference"><strong>{label}：</strong>{savedTime(value)}</p>;
  }
  if (reference === "freshness.clock_anomaly") return <p className="news-review-time-reference">保存的时间检查：{freshness?.clock_anomaly === true ? "来源时间存在异常，发布关系未确认。" : "未能确认该条时间异常记录。"}</p>;
  return <p className="news-review-unmapped">这项依据的位置无法确认，请查看已保存的来源材料；系统不会猜测对应原句。</p>;
}

function ReviewIssue({ issue, candidate, analysis, source, index }) {
  const claim = candidateText(candidate, issue?.field);
  const references = Array.isArray(issue?.evidence_fields) ? [...new Set(issue.evidence_fields.filter((entry) => plain(entry)))] : [];
  return <article className="news-review-card" aria-label={`待核对问题 ${index + 1}`}>
    <h4>{issueLabel(issue)}</h4>
    <h5>{claim.label}</h5>
    {claim.text ? <><blockquote className="news-review-claim">{claim.text}</blockquote>{claim.whole ? <p className="news-review-unmapped">问题定位到整段摘要，尚未精确到其中某一句；请不要把整段都当成已经判错。</p> : null}</> : <p className="news-review-unmapped">没有足够记录定位具体表述，无法确认哪句话需要核对；原输出如已保留，可在技术详情查看。</p>}
    {references.length ? references.map((reference) => <EvidenceReference key={reference} reference={reference} field={issue?.field} candidate={candidate} analysis={analysis} source={source} />) : <p className="news-review-unmapped">记录未提供具体来源对应关系；请查看已保存的材料，系统不会编造比对。</p>}
  </article>;
}

export function AnalysisReview({ analysis, source, originalUrl }) {
  if (!object(analysis)) return null;
  const review = object(analysis.semantic_review);
  const issues = Array.isArray(review?.issues) ? review.issues.filter(object) : [];
  const candidate = object(analysis.candidate_result);
  const freshness = object(analysis.freshness);
  const href = sourceHref(originalUrl);
  const needsReview = analysis.status === "needs_review";
  const savedPackage = analysis.evidence_package || candidate?.evidence_package;
  return <div className="news-reading-review">
    {!["completed", "success"].includes(analysis.status) && savedPackage ? <details className="news-analysis-used-scope"><summary>已使用范围（保存的分析记录）</summary><p>此范围属于已保存记录，结果是否完成或获接纳仍以分析状态为准。</p><EvidencePackageScope evidencePackage={savedPackage} /></details> : null}
    {needsReview ? <section className="news-semantic-review news-review-explanation" aria-label="需要核对的 AI 表述">
      <div role="status"><strong>发现可能误读，请先看这里</strong><p>这些表述暂时不能采信，尚未作为正常分析结果接纳。请先核对来源；本次预约已保留，不会自动重试或重新发起收费请求。</p></div>
      {issues.length ? issues.map((issue, index) => <ReviewIssue key={index} issue={issue} candidate={candidate} analysis={analysis} source={source} index={index} />) : <p className="news-review-unmapped">本次检查未能定位具体问题，不能视为内容已通过。现有记录不足以提供逐句比对。</p>}
      <p className="news-review-next"><strong>建议：</strong>先查看来源摘录或官方原文，暂不要依据这里的表述作判断。打开材料不会重新调用 AI。</p>
      {plain(source?.title) || plain(source?.excerpt) ? <details className="news-review-source"><summary>查看已保存的来源材料</summary><p>这是当前保存的标题与摘录；此分析记录未单独保存审核时的完整来源副本。</p>{plain(source?.title) ? <><h5>来源标题</h5><blockquote>{source.title}</blockquote></> : null}{plain(source?.excerpt) ? <><h5>来源摘录</h5><blockquote>{source.excerpt}</blockquote></> : null}</details> : null}
      {href ? <a className="news-button news-review-original" href={href} target="_blank" rel="noopener noreferrer">打开官方原文核对</a> : <p className="news-review-unmapped">官方原文链接不可用，可先核对已保存的来源材料。</p>}
    </section> : analysis.status === "unknown" || analysis.status === "failed" ? <div className="news-review-outcome" role="status"><strong>{analysis.status === "unknown" ? "本次结果未确认" : "这次分析未完成"}</strong><p>你仍可阅读已保存的消息和来源摘录。为避免重复收费，本次记录会保留，不会自动重试；这不表示供应商未计费。</p></div> : null}
    {freshness ? <details className="news-analysis-clock"><summary>查看发布时间与审核时刻</summary><dl><div><dt>{freshness.revision_after_discovery ? "当前版本标注时间" : "来源标注时间"}</dt><dd>{savedTime(freshness.published_at)}</dd></div><div><dt>首次发现</dt><dd>{savedTime(freshness.discovered_at)}</dd></div><div><dt>本次审核检查</dt><dd>{savedTime(analysis.quality_checked_at)}</dd></div></dl><p>{freshness.clock_anomaly ? "来源标注时间存在异常，发布时间与新鲜度尚未确认。" : freshness.revision_after_discovery ? "条目有更新，首次发现属于更早版本，不能替代当前版本发布时间。" : freshness.publication_age === "older_than_24h" ? "按保存的审核检查时间，来源标注时间已超过24小时；发现时间不代表事件发生时间。" : freshness.publication_age === "within_24h" ? "按保存的审核检查时间，来源标注时间距当次检查不超过24小时；未据此确认事件实际发生时间。" : "发布时间或时间关系尚未确认。"}</p></details> : null}
    {typeof analysis.raw_model_output === "string" ? <details className="news-analysis-raw"><summary>技术详情：模型原输出（未作修正）</summary><p>保留原文供核对；它不是已经接纳的分析结论。展开不会发送模型请求。</p><pre>{analysis.raw_model_output}</pre></details> : null}
  </div>;
}
