import { AlertTriangle, ArrowDownRight, CheckCircle2, ChevronDown, ClipboardCheck, Search, ShieldCheck, X } from "lucide-react";
import { memo, useCallback, useDeferredValue, useEffect, useId, useMemo, useRef, useState } from "react";
import { api } from "../api";
import {
  normalizeProjectReadinessResponse,
  projectReadinessErrorMessage,
  projectReadinessLoadPlan,
  projectReadinessPresentation,
} from "../projectReadiness";
import "../styles/project-readiness-refinement.css";

const GAP_PAGE_SIZE = 40;
const COMPACT_GAP_PAGE_SIZE = 16;
const COMPACT_GAP_QUERY = "(max-width: 620px)";
const READINESS_REQUEST_CACHE_LIMIT = 32;
const readinessRequestCache = new Map();

function cachedProjectReadinessRequest({ requestKey, roomId, artifactId, artifactVersion }) {
  const cached = readinessRequestCache.get(requestKey);
  if (cached) return cached;
  const request = api.projectReadiness(roomId, artifactId, artifactVersion).catch((error) => {
    if (readinessRequestCache.get(requestKey) === request) {
      readinessRequestCache.delete(requestKey);
    }
    throw error;
  });
  readinessRequestCache.set(requestKey, request);
  if (readinessRequestCache.size > READINESS_REQUEST_CACHE_LIMIT) {
    const oldestKey = readinessRequestCache.keys().next().value;
    readinessRequestCache.delete(oldestKey);
  }
  return request;
}

function shortHash(value) {
  const hash = String(value || "").trim();
  return /^[0-9a-f]{64}$/i.test(hash) ? `${hash.slice(0, 10)}…${hash.slice(-8)}` : "未封印";
}

function initialState() {
  return { status: "idle", projection: null, error: "" };
}

function currentGapPageSize() {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
    return GAP_PAGE_SIZE;
  }
  return window.matchMedia(COMPACT_GAP_QUERY).matches
    ? COMPACT_GAP_PAGE_SIZE
    : GAP_PAGE_SIZE;
}

const GapList = memo(function GapList({ expanded, group, onToggle, sectionId }) {
  const { emptyText, eyebrow, order, rows, status, statusLabel, title, tone } = group;
  const bodyId = `${sectionId}-body`;
  const listId = `${sectionId}-rows`;
  const statusId = `${sectionId}-status`;
  const titleId = `${sectionId}-title`;
  const [pageSize, setPageSize] = useState(currentGapPageSize);
  const pageSizeRef = useRef(pageSize);
  const [rowLimit, setRowLimit] = useState(pageSize);
  const moreButtonRef = useRef(null);
  useEffect(() => {
    setRowLimit(pageSizeRef.current);
  }, [rows]);
  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
      return undefined;
    }
    const media = window.matchMedia(COMPACT_GAP_QUERY);
    const syncPageSize = () => {
      const previousPageSize = pageSizeRef.current;
      const nextPageSize = media.matches ? COMPACT_GAP_PAGE_SIZE : GAP_PAGE_SIZE;
      if (previousPageSize === nextPageSize) return;
      pageSizeRef.current = nextPageSize;
      setPageSize(nextPageSize);
      setRowLimit((current) => (
        current === previousPageSize || current < nextPageSize ? nextPageSize : current
      ));
    };
    syncPageSize();
    if (typeof media.addEventListener === "function") {
      media.addEventListener("change", syncPageSize);
      return () => media.removeEventListener("change", syncPageSize);
    }
    media.addListener(syncPageSize);
    return () => media.removeListener(syncPageSize);
  }, []);
  const visibleRows = useMemo(() => rows.slice(0, rowLimit), [rowLimit, rows]);
  const remainingRows = Math.max(0, rows.length - visibleRows.length);
  const nextPageSize = Math.min(pageSize, remainingRows);
  const visibleRatio = rows.length ? Math.round((visibleRows.length / rows.length) * 100) : 100;
  const listComplete = rows.length > pageSize && remainingRows === 0;
  return (
    <section
      id={sectionId}
      className={`project-readiness-gap-group ${tone}`}
      data-gate-state={status}
      data-expanded={expanded ? "true" : "false"}
      aria-labelledby={titleId}
      tabIndex={-1}
    >
      <header>
        <div className="project-readiness-gap-title">
          <b aria-hidden="true">{order}</b>
          <div><small>{eyebrow}</small><h4 id={titleId}>{title}</h4></div>
        </div>
        <em><span>{statusLabel}</span>{String(rows.length).padStart(2, "0")}</em>
        <button
          type="button"
          className="project-readiness-gap-toggle"
          aria-controls={bodyId}
          aria-expanded={expanded}
          aria-label={`${expanded ? "收起" : "展开"}${title}明细`}
          title={`${expanded ? "收起" : "展开"}${title}明细`}
          onClick={() => onToggle(group.key)}
        >
          <ChevronDown size={16} aria-hidden="true" />
        </button>
      </header>
      <div id={bodyId} className="project-readiness-gap-body" hidden={!expanded}>
      {expanded ? (rows.length ? (
        <>
        <div
          id={statusId}
          className="project-readiness-gap-status"
          role="status"
          aria-live="polite"
          aria-atomic="true"
        >
          <span>展示 {visibleRows.length} / {rows.length} 项，只读顺序保持不变。</span>
          <span
            className="project-readiness-gap-progress"
            style={{ "--readiness-list-progress": `${visibleRatio}%` }}
            aria-hidden="true"
          ><i /></span>
        </div>
        <ul id={listId} aria-describedby={statusId}>
          {visibleRows.map((row) => (
            <li key={JSON.stringify([row.code, row.itemKey])}>
              <span><strong>{row.message}</strong><small>{row.itemKey || "产物整体"}</small></span>
              <code title={row.code}>{row.code}</code>
            </li>
          ))}
        </ul>
        {rows.length > pageSize ? (
          <button
            ref={moreButtonRef}
            type="button"
            className="secondary project-readiness-more"
            aria-controls={listId}
            data-list-action={listComplete ? "collapse" : "more"}
            aria-label={listComplete
              ? `收起到前 ${pageSize} 项，当前共 ${rows.length} 项`
              : `再显示 ${nextPageSize} 项，当前尚余 ${remainingRows} 项`}
            onClick={() => {
              if (listComplete) {
                setRowLimit(pageSize);
                requestAnimationFrame(() => {
                  moreButtonRef.current?.scrollIntoView({
                    block: "center",
                    inline: "nearest",
                  });
                });
                return;
              }
              setRowLimit((current) => current + pageSize);
            }}
          >
            <span>{listComplete ? `收起至前 ${pageSize} 项` : `再显示 ${nextPageSize} 项`}</span>
            <small>{listComplete ? `共 ${rows.length} 项` : `尚余 ${remainingRows} 项`}</small>
          </button>
        ) : null}
        </>
      ) : <p><CheckCircle2 size={13} aria-hidden="true" /><span>{emptyText}</span></p>) : null}
      </div>
    </section>
  );
});

export const ProjectReadinessPanel = memo(function ProjectReadinessPanel({
  room,
  artifact,
  slot,
  contribution,
  showLegacyFallback = false,
}) {
  const panelUid = useId().replace(/:/g, "");
  const headingId = `project-readiness-${panelUid}-heading`;
  const boundaryId = `project-readiness-${panelUid}-boundary`;
  const gateSectionId = (key) => `project-readiness-${panelUid}-${key}`;
  const plan = useMemo(() => projectReadinessLoadPlan({
    room,
    artifact,
    slot,
    contribution,
    showLegacyFallback,
  }), [artifact, contribution, room, showLegacyFallback, slot]);
  const [state, setState] = useState(initialState);
  const [expandedGateKeys, setExpandedGateKeys] = useState([]);
  const [gapQueryDraft, setGapQueryDraft] = useState("");
  const [gapQuery, setGapQuery] = useState("");
  const deferredGapQuery = useDeferredValue(gapQuery);
  const gapSearchComposingRef = useRef(false);
  const gapSearchInputRef = useRef(null);
  const requestKey = plan.requestKey;
  const requestRoomId = plan.expected?.roomId || "";
  const requestArtifactId = plan.expected?.artifactId || "";
  const requestArtifactVersion = plan.expected?.artifactVersion ?? null;
  const requestReady = plan.shouldLoad
    && Boolean(requestKey && requestRoomId && requestArtifactId)
    && Number.isSafeInteger(requestArtifactVersion);

  useEffect(() => {
    if (!requestReady) {
      setState(initialState);
      setExpandedGateKeys([]);
      return undefined;
    }
    let cancelled = false;
    setState({ status: "loading", projection: null, error: "" });
    setExpandedGateKeys([]);
    setGapQueryDraft("");
    setGapQuery("");
    cachedProjectReadinessRequest({
      requestKey,
      roomId: requestRoomId,
      artifactId: requestArtifactId,
      artifactVersion: requestArtifactVersion,
    }).then((payload) => {
      if (cancelled) return;
      const projection = normalizeProjectReadinessResponse(payload, {
        ...plan.expected,
        roomId: requestRoomId,
        artifactId: requestArtifactId,
        artifactVersion: requestArtifactVersion,
      });
      const firstExpandedGateKey = projection.valid
        ? projection.blockers.length
          ? "blockers"
          : projection.structuralGaps.length
            ? "structural"
            : projection.evidenceGaps.length
              ? "evidence"
              : "blockers"
        : "";
      setExpandedGateKeys(firstExpandedGateKey ? [firstExpandedGateKey] : []);
      setState({
        status: projection.valid ? "ready" : "integrity_failed",
        projection,
        error: "",
      });
    }).catch((error) => {
      if (cancelled || error?.name === "AbortError") return;
      setState({
        status: "error",
        projection: null,
        error: projectReadinessErrorMessage(error),
      });
      setExpandedGateKeys([]);
    });
    return () => {
      cancelled = true;
    };
  }, [requestArtifactId, requestArtifactVersion, requestKey, requestReady, requestRoomId]);

  const toggleGate = useCallback((key) => {
    setExpandedGateKeys((current) => (
      current.includes(key)
        ? current.filter((candidate) => candidate !== key)
        : [...current, key]
    ));
  }, []);
  const revealGate = useCallback((key, sectionId) => {
    setExpandedGateKeys((current) => (
      current.includes(key) ? current : [...current, key]
    ));
    requestAnimationFrame(() => {
      document.getElementById(sectionId)?.focus({ preventScroll: true });
    });
  }, []);

  const projection = state.projection;
  const presentation = useMemo(
    () => projectReadinessPresentation(projection),
    [projection],
  );
  const normalizedGapQuery = deferredGapQuery.trim().toLowerCase();
  const gapQueryPending = gapQueryDraft !== gapQuery || deferredGapQuery !== gapQuery;
  const filteredGroups = useMemo(() => presentation.groups.map((group) => {
    if (!normalizedGapQuery) return group;
    const rows = group.rows.filter((row) => (
      row.message.toLowerCase().includes(normalizedGapQuery)
      || row.itemKey.toLowerCase().includes(normalizedGapQuery)
      || row.code.toLowerCase().includes(normalizedGapQuery)
    ));
    return {
      ...group,
      rows,
      statusLabel: "MATCH",
      emptyText: "当前定位词未命中此组；原始缺口未被修改。",
    };
  }), [normalizedGapQuery, presentation.groups]);
  const matchedGapCount = filteredGroups.reduce((total, group) => total + group.rows.length, 0);
  const matchingGateSignature = filteredGroups
    .filter((group) => group.rows.length)
    .map((group) => group.key)
    .join(":");
  const defaultExpandedGateKey = presentation.groups.find((group) => group.rows.length)?.key
    || presentation.groups[0]?.key
    || "";
  useEffect(() => {
    if (state.status !== "ready" || !presentation.visible) return;
    if (normalizedGapQuery) {
      setExpandedGateKeys(matchingGateSignature ? matchingGateSignature.split(":") : []);
      return;
    }
    setExpandedGateKeys(defaultExpandedGateKey ? [defaultExpandedGateKey] : []);
  }, [defaultExpandedGateKey, matchingGateSignature, normalizedGapQuery, presentation.visible, state.status]);
  const updateGapQuery = useCallback((value) => {
    const bounded = String(value || "").slice(0, 120);
    setGapQueryDraft(bounded);
    if (!gapSearchComposingRef.current) setGapQuery(bounded);
  }, []);
  const finishGapQueryComposition = useCallback((value) => {
    const bounded = String(value || "").slice(0, 120);
    gapSearchComposingRef.current = false;
    setGapQueryDraft(bounded);
    setGapQuery(bounded);
  }, []);
  const clearGapQuery = useCallback(() => {
    gapSearchComposingRef.current = false;
    gapSearchInputRef.current?.focus();
    setGapQueryDraft("");
    setGapQuery("");
  }, []);
  const panelState = state.status === "ready" ? presentation.state : state.status;
  if (!plan.visible) return null;

  return (
    <section
      className={`project-readiness-panel readiness-docket ${plan.shouldLoad ? "available" : "read-only"}`}
      data-readiness-state={panelState}
      aria-labelledby={headingId}
      aria-describedby={boundaryId}
      aria-busy={plan.shouldLoad && state.status === "loading"}
    >
      <header className="project-readiness-heading">
        <div className="project-readiness-heading-copy">
          <small className="project-readiness-kicker">IMPLEMENTATION PRE-FLIGHT</small>
          <div className="project-readiness-heading-title">
            <ClipboardCheck size={18} aria-hidden="true" />
            <h3 id={headingId}>项目实施前结构复核</h3>
          </div>
          <p>只检查精确产物版本与证据图的合同内缺口；不修改产物，也不核发部署、发布或执行权限。</p>
        </div>
        <em><span>ARTIFACT</span>v{plan.expected?.artifactVersion || (Number.isSafeInteger(artifact?.version) ? artifact.version : "?")} · READ ONLY</em>
      </header>

      {!plan.shouldLoad ? (
        <p className="project-readiness-state warning" role="note">
          <AlertTriangle size={15} aria-hidden="true" />
          <span><strong>当前不生成项目就绪指标</strong><small>{plan.reason}</small></span>
        </p>
      ) : null}
      {plan.shouldLoad && state.status === "loading" ? (
        <p className="project-readiness-state" aria-live="polite">
          正在读取绑定 v{plan.expected.artifactVersion} 的确定性结构投影……
        </p>
      ) : null}
      {plan.shouldLoad && state.status === "error" ? (
        <p className="project-readiness-state error" role="alert">
          <AlertTriangle size={15} aria-hidden="true" />
          <span><strong>投影读取失败，未展示任何指标</strong><small>{state.error}</small></span>
        </p>
      ) : null}
      {plan.shouldLoad && state.status === "integrity_failed" ? (
        <p className="project-readiness-state error" role="alert">
          <AlertTriangle size={15} aria-hidden="true" />
          <span>
            <strong>投影绑定或安全字段校验失败</strong>
            <small>{projection?.issues?.[0] || "完整性无法验证；全部指标已隐藏。"}</small>
          </span>
        </p>
      ) : null}

      {plan.shouldLoad && state.status === "ready" && projection?.valid && presentation.visible ? (
        <>
          <div
            className={`project-readiness-summary ${presentation.state}`}
            role="status"
            aria-live="polite"
          >
            {presentation.state === "ready"
              ? <ShieldCheck size={19} aria-hidden="true" />
              : <AlertTriangle size={19} aria-hidden="true" />}
            <span className="project-readiness-summary-copy">
              <small>{presentation.eyebrow}</small>
              <strong>{presentation.headline}</strong>
              <span>{presentation.description}</span>
            </span>
            <em aria-label={`共 ${presentation.totalGapCount} 项合同内缺口`}>
              <strong>{String(presentation.totalGapCount).padStart(2, "0")}</strong>
              <small>记录总数</small>
            </em>
          </div>
          <div className="project-readiness-scope-ledger" role="list" aria-label="本次结构复核边界">
            <span role="listitem"><small>复核输入</small><strong>冻结版本</strong></span>
            <span role="listitem"><small>外部调用 / 写入</small><strong>均为 0</strong></span>
            <span role="listitem"><small>授权结论</small><strong>不产生</strong></span>
          </div>
          <div
            className="project-readiness-locator"
            role="search"
            aria-label="在合同内缺口中定位"
            aria-busy={gapQueryPending}
            data-query-active={gapQueryDraft.trim() ? "true" : "false"}
          >
            <label htmlFor={`project-readiness-${panelUid}-gap-query`}>
              <Search size={15} aria-hidden="true" />
              <span><small>READ-ONLY LOCATOR</small><strong>定位合同内缺口</strong></span>
            </label>
            <div className="project-readiness-locator-field">
              <input
                ref={gapSearchInputRef}
                id={`project-readiness-${panelUid}-gap-query`}
                type="search"
                value={gapQueryDraft}
                maxLength={120}
                autoComplete="off"
                spellCheck="false"
                placeholder="搜索消息、对象键或缺口代码"
                aria-describedby={`project-readiness-${panelUid}-gap-query-status`}
                onChange={(event) => updateGapQuery(event.currentTarget.value)}
                onCompositionStart={() => { gapSearchComposingRef.current = true; }}
                onCompositionEnd={(event) => finishGapQueryComposition(event.currentTarget.value)}
              />
              {gapQueryDraft ? (
                <button
                  type="button"
                  className="project-readiness-locator-clear"
                  aria-label="清除缺口定位词"
                  onClick={clearGapQuery}
                >
                  <X size={15} aria-hidden="true" />
                </button>
              ) : null}
            </div>
            <p
              id={`project-readiness-${panelUid}-gap-query-status`}
              className="project-readiness-locator-status"
              data-empty={normalizedGapQuery && matchedGapCount === 0 ? "true" : "false"}
              role="status"
              aria-live="polite"
              aria-atomic="true"
            >
              {gapQueryPending
                ? "正在更新定位结果；当前只读投影未修改。"
                : normalizedGapQuery
                ? `命中 ${matchedGapCount} / ${presentation.totalGapCount} 项；仅筛选当前只读视图。`
                : `可定位 ${presentation.totalGapCount} 项合同内缺口；原始闸门计数保持不变。`}
            </p>
          </div>
          <ol className="project-readiness-metrics" aria-label="项目准备闸门顺序">
            {presentation.groups.map((group) => {
              const sectionId = gateSectionId(group.key);
              return (
                <li key={group.key} data-gate-state={group.status}>
                  <b aria-hidden="true">{group.order}</b>
                  <span><small>{group.eyebrow}</small><strong>{group.title}</strong></span>
                  <em><small>{group.statusLabel}</small><strong>{String(group.count).padStart(2, "0")}</strong></em>
                  <a
                    className="project-readiness-gate-jump"
                    href={`#${sectionId}`}
                    aria-label={`定位到第 ${group.order} 步：${group.title}明细`}
                    onClick={() => revealGate(group.key, sectionId)}
                  >
                    <ArrowDownRight size={16} aria-hidden="true" />
                  </a>
                </li>
              );
            })}
          </ol>
          <div className="project-readiness-gaps">
            {filteredGroups.map((group) => (
              <GapList
                key={`${projection.hashes.artifactSnapshotSha256}:${projection.hashes.evidenceGraphSha256}:${group.key}`}
                expanded={expandedGateKeys.includes(group.key)}
                group={group}
                onToggle={toggleGate}
                sectionId={gateSectionId(group.key)}
              />
            ))}
          </div>
          <details className="project-readiness-binding">
            <summary><ShieldCheck size={14} aria-hidden="true" />查看精确只读绑定</summary>
            <dl>
              <div><dt>产物快照</dt><dd><code title={projection.hashes.artifactSnapshotSha256}>{shortHash(projection.hashes.artifactSnapshotSha256)}</code></dd></div>
              <div><dt>证据图</dt><dd><code title={projection.hashes.evidenceGraphSha256}>{shortHash(projection.hashes.evidenceGraphSha256)}</code></dd></div>
              <div><dt>插件快照</dt><dd><code title={projection.hashes.pluginRegistrySnapshotSha256}>{shortHash(projection.hashes.pluginRegistrySnapshotSha256)}</code></dd></div>
              <div><dt>Adapter</dt><dd>{projection.resolution.adapter.adapter_id} · {projection.resolution.adapter.adapter_version}</dd></div>
              <div><dt>Port</dt><dd>{projection.resolution.port.port_id} · {projection.resolution.port.port_version}</dd></div>
            </dl>
          </details>
        </>
      ) : null}

      <p id={boundaryId} className="project-readiness-boundary">
        <ShieldCheck size={14} aria-hidden="true" />
        <span><strong>权限边界</strong>Provider 调用 0 · 市场读取 0 · 业务写入 0 · 不排名 · 不宣称赢家 · 不产生批准；最终决定始终由用户完成。</span>
      </p>
    </section>
  );
});
