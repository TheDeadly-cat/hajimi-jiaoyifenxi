import { evidenceRelationFlags } from "../artifactEvidenceSources";

const STATUS_ITEMS = [
  ["support", "支持"],
  ["counter", "反证"],
  ["conflict", "冲突"],
  ["gap", "缺口"],
];

export function EvidenceStatusStrip({ counts, source, audit, sourceDetail, compact = false }) {
  const values = counts || evidenceRelationFlags(source, audit, sourceDetail);
  return (
    <span className={`evidence-status-strip ${compact ? "compact" : ""}`} aria-label="证据关系状态">
      {STATUS_ITEMS.map(([key, label]) => {
        const value = counts ? Number(values[key] || 0) : values[key] ? 1 : 0;
        if (compact && !value) return null;
        return <span className={`${key} ${value ? "active" : ""}`} key={key}>{label}{counts ? ` ${value}` : ""}</span>;
      })}
    </span>
  );
}
