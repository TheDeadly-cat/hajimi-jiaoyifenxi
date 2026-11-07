const relevanceLabels = { direct: "直接相关", indirect: "间接相关", unrelated: "与当前关注无关" };
const noveltyLabels = { new_event: "新事件", material_revision: "实质修订", duplicate_confirmation: "重复确认或转载", historical_backfill: "历史回填" };
const eventLabels = { earnings: "财报", guidance: "指引", regulatory: "监管", macro: "宏观", industry: "行业信息", commentary: "评论或其他信息" };
const evidenceLabels = { full_text: "已保存正文", partial_text: "仅保存部分正文", excerpt_only: "只有摘录", headline_only: "只有标题", fetch_failed: "正文读取失败", access_restricted: "来源访问受限", retracted: "有撤回记录，需核对" };
const reasonLabels = {
  outside_focus: "不在当前关注范围内",
  not_important: "尚未达到本机规则的重要性条件",
  historical_backfill: "属于历史回填，需要另行核对",
  duplicate_confirmation: "属于重复确认或转载，保留已有依据",
  evidence_requires_review: "证据存在冲突、撤回或结构待核对",
  evidence_missing: "缺少可引用的正文或摘录",
  article_evidence_unavailable_no_send: "预算内没有可完整保留的证据组",
};
const label = (labels, value, fallback) => typeof value === "string" && Object.hasOwn(labels, value) ? labels[value] : fallback;

export function NewsPolicySummary({ policy }) {
  if (!policy || typeof policy !== "object" || Array.isArray(policy)) return null;
  const reasons = Array.isArray(policy.reasons) ? [...new Set(policy.reasons.map(reason => label(reasonLabels, reason, "部分筛选条件尚未确认，请核对材料与关注设置")))] : [];
  const eligible = policy.automatic_eligible === true && Array.isArray(policy.reasons) && !policy.reasons.length;
  const evidence = policy.evidence && typeof policy.evidence === "object" ? policy.evidence : {};
  const flags = [
    evidence.attachments_unread === true ? "附件未读，不能据此补足结论" : null,
    evidence.time_unknown === true ? "发布时间未知，不能把发现时间当发布时间" : null,
    evidence.conflict === true ? "资料存在冲突或撤回，需先核对" : null,
    evidence.selected_complete_groups === false ? "当前预算内没有完整可引用证据组" : null,
  ].filter(Boolean);
  const summary = eligible ? "符合本地筛选条件" : policy.automatic_eligible === false || reasons.length ? `暂不自动解释：${reasons.join("；") || "尚未满足当前筛选条件"}` : "自动筛选状态尚未确认";
  return <details className="news-policy-summary">
    <summary>{summary}</summary>
    <p>这是当前消息的本机规则分类，未调用模型。发送仍需当前工作模式、有效授权和可用额度；符合筛选条件不代表已经分析。</p>
    <dl aria-label="四维本机分类">
      <div><dt>相关性</dt><dd>{label(relevanceLabels, policy.relevance, "尚未判定")}</dd></div>
      <div><dt>新旧程度</dt><dd>{label(noveltyLabels, policy.novelty, "尚未判定")}</dd></div>
      <div><dt>事件类型</dt><dd>{label(eventLabels, policy.event_type, "尚未判定")}</dd></div>
      <div><dt>证据状态</dt><dd>{label(evidenceLabels, evidence.status, "读取状态尚未确认")}{flags.length ? <ul>{flags.map(flag => <li key={flag}>{flag}</li>)}</ul> : null}</dd></div>
    </dl>
    <p><strong>重要性（本机规则）：</strong>{policy.importance === "important" ? "重要，优先核对" : policy.importance === "ordinary" ? "普通" : "尚未分类"}。重要性用于筛选与排序，不代表可信程度或利多、利空。</p>
  </details>;
}
