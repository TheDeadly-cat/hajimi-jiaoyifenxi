import { AlertTriangle, ChevronRight, ExternalLink, Eye, Info, LoaderCircle, ShieldCheck } from "lucide-react";
import { useEffect, useId, useMemo, useRef, useState } from "react";
import { evidenceLocatorLabel, safeExternalUrl } from "../artifactEvidenceSources";
import { evidenceSourcePreviewPresentation } from "../evidenceUi";
import "../styles/evidence-ui.css";
import "../styles/evidence-source-preview.css";

const PREVIEW_LIMIT = 12000;
const TEXT_LIMIT = 1000;

function boundedText(value, fallback = "", limit = TEXT_LIMIT) {
  const text = String(value ?? "").trim();
  const normalized = text || fallback;
  return normalized.length <= limit ? normalized : normalized.slice(0, limit) + "...";
}

function boundedPreview(value) {
  const text = typeof value === "string" ? value : "";
  if (text.length <= PREVIEW_LIMIT) {
    return { text, truncated: false, originalLength: text.length, visibleLength: text.length };
  }
  return {
    text: text.slice(0, PREVIEW_LIMIT) + "...",
    truncated: true,
    originalLength: text.length,
    visibleLength: PREVIEW_LIMIT,
  };
}

function safeStatusToken(value) {
  const token = String(value || "")
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 48);
  return token || "preview";
}

function sourceIdentity(item, citedVersion) {
  return JSON.stringify([
    "evidence-source-preview",
    item?.id,
    item?.evidence_id,
    item?.sourceId,
    item?.sourceUrl,
    citedVersion,
  ]);
}

function loadErrorMessage(error) {
  const message = typeof error?.message === "string" ? error.message : "";
  return boundedText(message, "冻结来源详情加载失败，请重试。");
}

export function EvidenceSourcePreview({
  item,
  citedVersion,
  sourceDetail,
  sourceDetailRequired,
  onLoadSourceDetail,
  selected = false,
}) {
  const previewId = useId();
  const previewMetaId = `${previewId}-preview-meta`;
  const previewBodyId = `${previewId}-preview-body`;
  const safeItem = item && typeof item === "object" ? item : {};
  const identity = sourceIdentity(safeItem, citedVersion);
  const [expanded, setExpanded] = useState(Boolean(selected));
  const [localBusy, setLocalBusy] = useState(false);
  const [localError, setLocalError] = useState("");
  const identityRef = useRef(identity);
  const requestIdRef = useRef(0);
  const inFlightRef = useRef(false);
  const mountedRef = useRef(false);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      requestIdRef.current += 1;
      inFlightRef.current = false;
    };
  }, []);

  useEffect(() => {
    requestIdRef.current += 1;
    inFlightRef.current = false;
    setLocalBusy(false);
    setLocalError("");
  }, [identity]);

  useEffect(() => {
    const identityChanged = identityRef.current !== identity;
    identityRef.current = identity;
    setExpanded((current) => {
      if (identityChanged) return Boolean(selected);
      return selected ? true : current;
    });
  }, [identity, selected]);

  const sourceUrl = useMemo(() => safeExternalUrl(
    sourceDetailRequired ? sourceDetail?.sourceUrl : safeItem.sourceUrl,
  ), [safeItem.sourceUrl, sourceDetail?.sourceUrl, sourceDetailRequired]);
  const locatorLabel = useMemo(
    () => evidenceLocatorLabel(safeItem.locator),
    [safeItem.locator],
  );
  const view = useMemo(() => evidenceSourcePreviewPresentation({
    item: safeItem,
    sourceDetail,
    sourceDetailRequired,
    selected,
    locatorLabel,
    sourceUrl,
  }), [locatorLabel, safeItem, selected, sourceDetail, sourceDetailRequired, sourceUrl]);

  if (!view.visible) return null;

  const status = safeStatusToken(view.status);
  const loading = localBusy || status === "loading";
  const displayError = localError || boundedText(view.error);
  const preview = boundedPreview(view.preview);
  const externalUrl = safeExternalUrl(view.sourceUrl);

  const toggleDetailsFromKeyboard = (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    setExpanded((current) => !current);
  };

  const loadSourceDetail = async () => {
    if (typeof onLoadSourceDetail !== "function" || !view.canLoad || inFlightRef.current) return;
    const requestId = requestIdRef.current + 1;
    requestIdRef.current = requestId;
    inFlightRef.current = true;
    setLocalBusy(true);
    setLocalError("");
    try {
      await onLoadSourceDetail(safeItem, citedVersion);
    } catch (requestError) {
      if (mountedRef.current && requestIdRef.current === requestId) {
        setLocalError(loadErrorMessage(requestError));
      }
    } finally {
      if (requestIdRef.current === requestId) inFlightRef.current = false;
      if (mountedRef.current && requestIdRef.current === requestId) setLocalBusy(false);
    }
  };

  return (
    <details
      className="artifact-source-preview evidence-source-dossier evidence-source-dossier-v2"
      data-source-status={status}
      data-source-selected={selected ? "true" : "false"}
      open={expanded}
      onToggle={(event) => setExpanded(event.currentTarget.open)}
      aria-busy={loading}
    >
      <summary aria-controls={previewBodyId} aria-expanded={expanded} onKeyDown={toggleDetailsFromKeyboard}>
        <ChevronRight className="evidence-source-chevron" size={15} aria-hidden="true" />
        <span className="evidence-source-summary-copy">
          <small><Eye size={11} aria-hidden="true" />SOURCE DOSSIER</small>
          <strong>{boundedText(view.summaryLabel, "来源预览", 240)}</strong>
        </span>
        <em>{boundedText(view.statusLabel, "预览状态", 80)}</em>
      </summary>
      <div className="evidence-source-dossier-body" id={previewBodyId}>
        {view.meta || view.locatorLabel ? (
          <div className="evidence-source-meta">
            {view.meta ? <small>{boundedText(view.meta, "", 500)}</small> : null}
            {view.locatorLabel ? <small className="artifact-source-locator">定位：{boundedText(view.locatorLabel, "", 500)}</small> : null}
          </div>
        ) : null}
        {view.showLoadControl ? (
          <button
            type="button"
            className="secondary compact evidence-source-load"
            disabled={!view.canLoad || typeof onLoadSourceDetail !== "function" || loading}
            aria-busy={loading}
            title={typeof onLoadSourceDetail !== "function" ? "当前视图未提供来源详情加载能力" : undefined}
            onClick={loadSourceDetail}
          >
            {loading
              ? <LoaderCircle className="spin" size={13} aria-hidden="true" />
              : <ShieldCheck size={13} aria-hidden="true" />}
            {loading ? "加载冻结来源中" : boundedText(view.loadButtonLabel, "加载冻结来源", 100)}
          </button>
        ) : null}
        {displayError ? <p className="evidence-source-error" role="alert"><AlertTriangle size={13} aria-hidden="true" /><span>{displayError}</span></p> : null}
        {view.boundaryNote ? (
          <p className="evidence-source-boundary" role="note">
            <ShieldCheck size={13} aria-hidden="true" /><span>{boundedText(view.boundaryNote)}</span>
          </p>
        ) : null}
        {view.notice ? <p className="evidence-source-notice" role="note"><Info size={13} aria-hidden="true" /><span>{boundedText(view.notice)}</span></p> : null}
        {preview.text ? (
          <section className="evidence-source-preview-text" aria-label="冻结来源文本">
            <header>
              <strong>冻结文本预览</strong>
              <small id={previewMetaId}>
                {preview.originalLength.toLocaleString("zh-CN")} 字符
                {preview.truncated ? ` · 显示前 ${preview.visibleLength.toLocaleString("zh-CN")} 字符` : ""}
              </small>
            </header>
            <pre tabIndex={0} aria-label="冻结来源预览" aria-describedby={previewMetaId}>{preview.text}</pre>
          </section>
        ) : null}
        {externalUrl ? (
          <a className="artifact-source-url evidence-source-external" href={externalUrl} target="_blank" rel="noopener noreferrer">
            打开外部来源（新窗口）<ExternalLink size={12} aria-hidden="true" />
          </a>
        ) : null}
      </div>
    </details>
  );
}
