import { AlertTriangle, CheckCircle2, ChevronDown, ExternalLink, LoaderCircle, RefreshCw, ShieldCheck, Upload } from "lucide-react";
import { memo, useId, useMemo, useState } from "react";
import {
  deriveOfficialSupplementCandidates,
  deriveStorageReadinessView,
  storageCoverageText,
  storageSourceStateLabel,
} from "../storageReadiness";
import "../styles/storage-data-readiness-polish.css";

const EMPTY_LIST = Object.freeze([]);

function formatSymbol(value) {
  const symbol = typeof value === "string" ? value.trim() : "";
  return symbol ? symbol.replace(/^US\./, "") : "未知标的";
}

export const StorageDataReadinessPanel = memo(function StorageDataReadinessPanel({ status, readiness, loading, gate, onRefresh, onAddOfficialSupplement }) {
  const view = useMemo(
    () => deriveStorageReadinessView(status, readiness, gate),
    [gate, readiness, status],
  );
  const blockedActions = Array.isArray(view.convergence.blockers)
    ? view.convergence.blockers
    : EMPTY_LIST;
  const sources = Array.isArray(view.sources) ? view.sources : EMPTY_LIST;
  const supplementCandidates = useMemo(
    () => deriveOfficialSupplementCandidates(readiness),
    [readiness],
  );
  const actionsId = `${useId()}-readiness-actions`;
  const [actionDisclosure, setActionDisclosure] = useState(() => ({
    checked: view.checked,
    open: !view.checked,
  }));
  const actionsExpanded = actionDisclosure.checked === view.checked
    ? actionDisclosure.open
    : !view.checked;
  const fullyVerified = Boolean(view.roundAdmission.ready && view.convergence.ready);
  const refreshAvailable = typeof onRefresh === "function";
  const supplementAvailable = typeof onAddOfficialSupplement === "function";
  const HeaderIcon = fullyVerified ? ShieldCheck : AlertTriangle;
  const summary = view.checked
    ? fullyVerified
      ? "本地行情准入与研究证据已核验"
      : view.convergence.preparation_usable
        ? "正式讨论仍受阻，但可先准备官方资料"
        : "正式讨论与证据准备仍有缺口"
    : view.roundAdmission.ready
      ? "本轮四股行情准入已通过；刷新后核验独立公开证据"
      : "连接状态已检测；刷新后核验真实覆盖";

  return (
    <section
      className={`storage-data-readiness storage-data-readiness-workbench ${fullyVerified ? "ready" : "blocked"}`}
      data-readiness-state={fullyVerified ? "verified" : "blocked"}
      aria-label="存储产业研究数据准入核验中心"
      aria-busy={loading}
    >
      <header>
        <span><HeaderIcon aria-hidden="true" size={13} />数据准入核验中心</span>
        <strong>{view.readyCount}/{view.totalCount} 已核验{view.partialCount ? ` · ${view.partialCount} 部分` : ""}</strong>
      </header>
      <p>{summary}</p>
      <div className="storage-readiness-source-list" role="list" aria-label="数据来源核验状态">
        {sources.map((source) => (
          <div className={`storage-readiness-source ${source.state}`} key={source.id} role="listitem">
            {source.state === "ready" ? <CheckCircle2 aria-hidden="true" size={12} /> : <AlertTriangle aria-hidden="true" size={12} />}
            <span><strong>{source.label}</strong><small>{storageSourceStateLabel(source)}</small></span>
            <b>{storageCoverageText(source)}</b>
          </div>
        ))}
      </div>
      {blockedActions.length ? (
        <details
          className="storage-readiness-actions"
          open={actionsExpanded}
          onToggle={(event) => setActionDisclosure({ checked: view.checked, open: event.currentTarget.open })}
        >
          <summary aria-controls={actionsId} aria-expanded={actionsExpanded}>
            <span>需要处理的前置条件 · {blockedActions.length}</span>
            <ChevronDown className="storage-readiness-chevron" size={14} aria-hidden="true" />
          </summary>
          <ol id={actionsId}>
            {blockedActions.map((blocker) => (
              <li key={blocker.source_id}>
                <strong>{blocker.label}</strong><span>{blocker.action}</span>
                {blocker.source_id === "earnings_materials" && supplementCandidates.length ? (
                  <div className="official-supplement-candidates" role="list" aria-label="可人工补充的官方业绩文件">
                    {supplementCandidates.map((candidate) => (
                      <article key={candidate.id} role="listitem">
                        <span>
                          <b>{formatSymbol(candidate.symbol)} · {candidate.fiscal_period}</b>
                          <small>{candidate.title}</small>
                        </span>
                        <span className="official-supplement-actions">
                          <a href={candidate.official_url} target="_blank" rel="noreferrer" title={candidate.official_url} aria-label={`打开 ${formatSymbol(candidate.symbol)} 官方文件链接`}>
                            <ExternalLink aria-hidden="true" size={10} />官方链接
                          </a>
                          <button type="button" onClick={() => onAddOfficialSupplement?.(candidate)} disabled={!supplementAvailable}>
                            <Upload aria-hidden="true" size={10} />上传已下载文件
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
      <button type="button" className="storage-readiness-refresh" onClick={onRefresh} disabled={loading || !refreshAvailable} aria-busy={loading}>
        {loading ? <LoaderCircle aria-hidden="true" className="spin" size={12} /> : <RefreshCw aria-hidden="true" size={12} />}
        {loading ? "正在核验公开来源…" : view.checked ? "刷新官方资料" : "准备独立官方证据"}
      </button>
      <small className="storage-readiness-boundary">
        证据准备不会放宽 Futu 四股准入门；仅使用只读行情和公开资料，无账户、委托或下单能力。
      </small>
    </section>
  );
});
