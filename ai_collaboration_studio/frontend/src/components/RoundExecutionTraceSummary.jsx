import { Activity, AlertTriangle, ChevronRight, ShieldCheck } from "lucide-react";
import {
  roundExecutionTraceAnchorState,
  roundExecutionTraceSummaryText,
} from "../roundExecutionTrace";

function integrityLabel(trace) {
  if (!trace) return "按需读取";
  if (!trace.valid || trace.integrity?.status === "invalid") return "校验未通过";
  if (trace.integrity?.status === "verified" && trace.integrity?.ok) return "完整性已核验";
  return "部分历史记录";
}

export function RoundExecutionTraceSummary({
  roundId,
  roundStatus = "",
  trace = null,
  loading = false,
  error = "",
  stale = false,
  onOpen,
}) {
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

  return (
    <button
      type="button"
      className={`round-trace-summary${hasProblem ? " warning" : ""}`}
      onClick={() => onOpen?.(roundId)}
      aria-label={`查看本轮执行轨迹：${detail}`}
    >
      <span className="round-trace-summary-icon"><Activity size={15} aria-hidden="true" /></span>
      <span className="round-trace-summary-copy">
        <span><strong>本轮执行轨迹</strong>{roundStatus ? <em>{roundStatus}</em> : null}</span>
        <small>{detail}</small>
        <small className="round-trace-summary-integrity">
          <IntegrityIcon size={11} aria-hidden="true" />
          {integrityLabel(trace)}{anchorLabel ? ` · 锚点${anchorLabel}` : ""}{stale ? " · 有新记录" : ""} · 打开轨迹不会调用模型
        </small>
      </span>
      <ChevronRight size={16} aria-hidden="true" />
    </button>
  );
}
