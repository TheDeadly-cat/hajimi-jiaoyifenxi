import { Eye } from "lucide-react";
import { evidenceLocatorLabel, safeExternalUrl } from "../artifactEvidenceSources";

export function EvidenceSourcePreview({
  item,
  citedVersion,
  sourceDetail,
  sourceDetailRequired,
  onLoadSourceDetail,
  selected = false,
}) {
  const sourcePreview = sourceDetailRequired
    ? sourceDetail?.preview || item.preview
    : item.preview;
  const sourceMeta = sourceDetailRequired
    ? sourceDetail?.sourceMeta || item.sourceMeta
    : item.sourceMeta;
  const sourceUrl = safeExternalUrl(
    sourceDetailRequired ? sourceDetail?.sourceUrl || item.sourceUrl : item.sourceUrl,
  );
  const locatorLabel = evidenceLocatorLabel(item.locator);
  const hasDisplayContent = Boolean(
    sourcePreview || sourceMeta || sourceUrl || item.previewComplete !== true || item.type === "material",
  );
  if (!hasDisplayContent) return null;

  return (
    <details
      className="artifact-source-preview"
    >
      <summary><Eye size={12} />{selected ? "查看被引用的精确来源" : "预览权威精确来源"}</summary>
      {sourceMeta ? <small>{sourceMeta}</small> : null}
      {locatorLabel ? <small className="artifact-source-locator">定位：{locatorLabel}</small> : null}
      {sourceDetailRequired ? (
        <button
          type="button"
          className="secondary compact"
          disabled={["loading", "ready"].includes(sourceDetail?.status)}
          onClick={() => onLoadSourceDetail(item, citedVersion)}
        >
          {sourceDetail?.status === "loading"
            ? "正在加载完整冻结来源…"
            : sourceDetail?.status === "ready"
              ? "完整冻结来源已加载"
              : "加载完整冻结来源"}
        </button>
      ) : null}
      {sourceDetail?.status === "error" ? <p className="artifact-source-preview-error">{sourceDetail.error}</p> : null}
      {sourceDetailRequired && !sourceDetail ? <p>完整内容只从该产物绑定的冻结轮次读取，不会访问实时市场或用最新版替代。</p> : null}
      {sourceDetail?.status === "ready" && sourceDetail.previewComplete !== true ? (
        <p className="artifact-source-preview-error">完整来源仍因硬上限或凭证脱敏而不完整，不能标记为已核对。</p>
      ) : null}
      {item.type !== "material"
        && item.previewComplete !== true
        && sourceDetail?.previewComplete !== true ? (
        <p className="artifact-source-preview-error">
          当前仅有截断、脱敏或预算受限的来源预览，暂不能标记为已核对；外部链接也不会自动视为已核验。
        </p>
      ) : null}
      {sourcePreview ? <pre>{sourcePreview}</pre> : null}
      {sourceUrl ? <a className="artifact-source-url" href={sourceUrl} target="_blank" rel="noreferrer">打开外部来源</a> : null}
    </details>
  );
}
