import React from "react";

export function MaterialQuality({ quality }) {
  if (!quality) return null;
  const warnings = Array.isArray(quality.warnings) ? quality.warnings : [];
  return <section className="news-material-quality" aria-label="阅读前核对">
    <h3>阅读前核对 <span>本机检查 · 不消耗模型额度</span></h3>
    <p className={quality.status === "clock_anomaly" || quality.can_analyze === false ? "news-source-error" : ""}>{quality.label || "仅有订阅标题与摘录"}</p>
    {warnings.length ? <ul>{warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul> : null}
    {quality.can_analyze === false ? <p>请先打开官方原文核对；当前材料不会发送给模型。</p> : null}
  </section>;
}

export function AnalysisEvidence({ analysis }) {
  const evidence = Array.isArray(analysis?.evidence) ? analysis.evidence : [];
  if (analysis?.legacy) return <p className="news-analysis-legacy">这是保留的旧版分析，尚未经过新规则检查；不会自动重新分析或扣费。</p>;
  if (!evidence.length) return null;
  return <details className="news-analysis-evidence">
    <summary>查看要点依据原句（{evidence.length}）</summary>
    <p>引文与已保存材料对应，仅说明出处；数字含义、条件与结论仍需人工核对。全文尚未读取。</p>
    <ol>{evidence.map((entry, index) => <li key={index}>
      <strong>{entry.claim}</strong>
      <small>{entry.evidence_field === "title" ? "来源标题" : "来源摘录"}</small>
      <blockquote>{entry.quote}</blockquote>
    </li>)}</ol>
  </details>;
}
