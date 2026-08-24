import { Activity, AlertTriangle, ChevronRight, ShieldCheck } from "lucide-react";
import { memo, useId } from "react";
import {
  roundExecutionTraceAnchorState,
  roundExecutionTraceSummaryText,
} from "../roundExecutionTrace";
import "../styles/round-execution-trace-summary-polish.css";

function integrityLabel(trace) {
  if (!trace) return "按需读取";
  if (!trace.valid || trace.integrity?.status === "invalid") return "校验未通过";
  if (trace.integrity?.status === "verified" && trace.integrity?.ok) return "完整性已核验";
  return "部分历史记录";
}

export const RoundExecutionTraceSummary = memo(function RoundExecutionTraceSummary({
  roundId,
  roundStatus = "",
  trace = null,
  loading = false,
  error = "",
  stale = false,
  onOpen,
}) {
  const summaryId = useId();
  if (!roundId) return null;
  const hasProblem = Boolean(error) || (trace && !trace.valid);
  const anchorState = trace?.valid ? roundExecutionTraceAnchorState(trace) : null;
  const anchorLabel = anchorState
    ? `${anchorState.label}${anchorState.sequence ? ` #${anchorState.sequence}` : ""}`
    : "";
  const detail = loading
    ? "正在读取只读轨迹…"
    : error
      ? "读取失败，打开后可重试"
      : trace
        ? roundExecutionTraceSummaryText(trace)
        : "查看本轮调用、调度与决策链";
  const IntegrityIcon = hasProblem ? AlertTriangle : ShieldCheck;
  const openAvailable = typeof onOpen === "function";
  const visualState = hasProblem
    ? "warning"
    : loading
      ? "loading"
      : stale
        ? "stale"
        : trace
          ? "ready"
          : "idle";
  const stateLabel = {
    warning: "需要检查",
    loading: "读取中",
    stale: "有新记录",
    ready: "已载入",
    idle: "按需读取",
  }[visualState];
  const titleId = `${summaryId}-title`;
  const stateId = `${summaryId}-state`;
  const statusId = `${summaryId}-status`;
  const detailId = `${summaryId}-detail`;
  const integrityId = `${summaryId}-integrity`;
  const boundaryId = `${summaryId}-boundary`;
  const stateClasses = [
    "round-trace-summary",
    hasProblem ? "warning" : "",
    loading ? "loading" : "",
    stale ? "stale" : "",
  ].filter(Boolean).join(" ");

  return (
    <div className="round-trace-summary-container">
      <button
        type="button"
        aria-busy={loading}
        className={stateClasses}
        data-trace-state={visualState}
        onClick={() => onOpen?.(roundId)}
        disabled={!openAvailable}
        aria-labelledby={[titleId, stateId, roundStatus ? statusId : ""].filter(Boolean).join(" ")}
        aria-describedby={`${detailId} ${integrityId} ${boundaryId}`}
      >
        <span className="round-trace-summary-icon"><Activity size={15} aria-hidden="true" /></span>
        <span className="round-trace-summary-copy">
          <span className="round-trace-summary-heading">
            <span className="round-trace-summary-titleline">
              <strong id={titleId}>本轮执行轨迹</strong>
              <span id={stateId} className="round-trace-summary-state">{stateLabel}</span>
            </span>
            {roundStatus ? <em id={statusId}>{roundStatus}</em> : null}
          </span>
          <small id={detailId} className="round-trace-summary-detail">{detail}</small>
          <span className="round-trace-summary-meta">
            <small id={integrityId} className="round-trace-summary-integrity">
              <IntegrityIcon size={11} aria-hidden="true" />
              <span>{integrityLabel(trace)}{anchorLabel ? ` · 锚点${anchorLabel}` : ""}{stale ? " · 有新记录" : ""}</span>
            </small>
            <small id={boundaryId} className="round-trace-summary-boundary">
              <ShieldCheck size={11} aria-hidden="true" />
              只读打开 · 不调用模型
            </small>
          </span>
        </span>
        <span className="round-trace-summary-action" aria-hidden="true">
          <span>{openAvailable ? "查看" : "不可用"}</span>
          <ChevronRight size={16} />
        </span>
      </button>
    </div>
  );
});
