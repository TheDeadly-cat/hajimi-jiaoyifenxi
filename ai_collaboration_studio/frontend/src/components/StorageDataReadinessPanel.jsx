import { AlertTriangle, CheckCircle2, ExternalLink, LoaderCircle, RefreshCw, ShieldCheck, Upload } from "lucide-react";
import {
  deriveOfficialSupplementCandidates,
  deriveStorageReadinessView,
  storageCoverageText,
  storageSourceStateLabel,
} from "../storageReadiness";

export function StorageDataReadinessPanel({ status, readiness, loading, gate, onRefresh, onAddOfficialSupplement }) {
  const view = deriveStorageReadinessView(status, readiness, gate);
  const blockedActions = view.convergence.blockers || [];
  const supplementCandidates = deriveOfficialSupplementCandidates(readiness);
  const summary = view.checked
    ? view.roundAdmission.ready && view.convergence.ready
      ? "行情与研究证据均已就绪"
      : view.convergence.preparation_usable
        ? "正式讨论仍受阻，但可先准备官方资料"
        : "正式讨论与证据准备仍有缺口"
    : view.roundAdmission.ready
      ? "本轮四股行情准入已通过；刷新后核验独立公开证据"
      : "连接状态已检测；刷新后核验真实覆盖";

  return (
    <section className={`storage-data-readiness ${view.roundAdmission.ready && view.convergence.ready ? "ready" : "blocked"}`} aria-label="存储产业研究数据接入就绪中心">
      <header>
        <span><ShieldCheck size={13} />数据接入就绪中心</span>
        <strong>{view.readyCount}/{view.totalCount} 就绪{view.partialCount ? ` · ${view.partialCount} 部分` : ""}</strong>
      </header>
      <p>{summary}</p>
      <div className="storage-readiness-source-list">
        {view.sources.map((source) => (
          <div className={`storage-readiness-source ${source.state}`} key={source.id}>
            {source.state === "ready" ? <CheckCircle2 size={12} /> : <AlertTriangle size={12} />}
            <span><strong>{source.label}</strong><small>{storageSourceStateLabel(source)}</small></span>
            <b>{storageCoverageText(source)}</b>
          </div>
        ))}
      </div>
      {blockedActions.length ? (
        <details className="storage-readiness-actions" open={!view.checked}>
          <summary>需要处理的前置条件</summary>
          <ol>
            {blockedActions.map((blocker) => (
              <li key={blocker.source_id}>
                <strong>{blocker.label}</strong><span>{blocker.action}</span>
                {blocker.source_id === "earnings_materials" && supplementCandidates.length ? (
                  <div className="official-supplement-candidates" aria-label="可人工补充的官方业绩文件">
                    {supplementCandidates.map((candidate) => (
                      <article key={candidate.id}>
                        <span>
                          <b>{candidate.symbol.replace("US.", "")} · {candidate.fiscal_period}</b>
                          <small>{candidate.title}</small>
                        </span>
                        <span className="official-supplement-actions">
                          <a href={candidate.official_url} target="_blank" rel="noreferrer" title={candidate.official_url}>
                            <ExternalLink size={10} />官方链接
                          </a>
                          <button type="button" onClick={() => onAddOfficialSupplement?.(candidate)} disabled={!onAddOfficialSupplement}>
                            <Upload size={10} />上传已下载文件
                          </button>
                        </span>
                      </article>
                    ))}
                    <p>上传后先生成本机核验预览；只有你再次确认服务端返回的来源、文件与三项哈希，才会重新核验就绪状态。</p>
                  </div>
                ) : null}
              </li>
            ))}
          </ol>
        </details>
      ) : null}
      <button type="button" className="storage-readiness-refresh" onClick={onRefresh} disabled={loading || !onRefresh}>
        {loading ? <LoaderCircle className="spin" size={12} /> : <RefreshCw size={12} />}
        {loading ? "正在核验公开来源…" : view.checked ? "刷新官方资料" : "准备独立官方证据"}
      </button>
      <small className="storage-readiness-boundary">
        证据准备不会放宽 Futu 四股准入门；仅使用只读行情和公开资料，无账户、委托或下单能力。
      </small>
    </section>
  );
}
