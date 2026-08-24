import { evidenceRelationFlags } from "../artifactEvidenceSources";
import { evidenceStatusPresentation } from "../evidenceUi";
import { memo } from "react";
import "../styles/evidence-ui.css";
import "../styles/evidence-status-strip.css";

const TEXT_LIMIT = 160;

function boundedText(value, fallback) {
  const text = String(value ?? "").trim();
  const normalized = text || fallback;
  return normalized.length <= TEXT_LIMIT ? normalized : normalized.slice(0, TEXT_LIMIT) + "...";
}

function safeStatusToken(value) {
  const token = String(value || "")
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 48);
  return token || "unknown";
}

function relationCount(value) {
  const number = Number(value);
  return Number.isInteger(number) && number >= 0 ? number : 0;
}

export const EvidenceStatusStrip = memo(function EvidenceStatusStrip({
  counts,
  source,
  audit,
  sourceDetail,
  compact = false,
}) {
  const relationFlags = evidenceRelationFlags(source, audit, sourceDetail);
  const view = evidenceStatusPresentation({
    counts,
    relationFlags,
    compact,
  });
  const items = Array.isArray(view?.items) ? view.items : [];
  const showValues = counts !== null && counts !== undefined;
  const ariaLabel = boundedText(view?.ariaLabel, "证据关系状态");

  return (
    <>
      <span
        className={"evidence-status-strip evidence-status-strip-v2" + (compact ? " compact" : "")}
        data-relation-count={relationCount(view?.total)}
        role="list"
        aria-label="证据关系状态"
      >
        {items.map((item) => {
          const token = safeStatusToken(item?.key);
          const label = boundedText(item?.label, "状态未命名");
          return (
            <span
              className={"evidence-status-item " + (item?.active ? "active" : "inactive")}
              data-status={token}
              data-active={item?.active ? "true" : "false"}
              role="listitem"
              key={`evidence-status:${item.key}`}
            >
              <i aria-hidden="true" />
              <span>{label}</span>
              {showValues ? <strong>{boundedText(item?.value, "0")}</strong> : null}
            </span>
          );
        })}
        {view?.empty ? <span className="evidence-status-empty" role="listitem"><i aria-hidden="true" /><span>未标注</span></span> : null}
      </span>
      <span
        className="evidence-status-announcement"
        role="status"
        aria-live="polite"
        aria-atomic="true"
      >
        {ariaLabel}
      </span>
    </>
  );
});
