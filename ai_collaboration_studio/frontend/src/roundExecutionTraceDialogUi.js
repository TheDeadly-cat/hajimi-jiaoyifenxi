import { normalizeRoundExecutionTrace } from "./roundExecutionTrace.js";

export const ROUND_TRACE_INITIAL_EVENT_WINDOW = 100;
export const ROUND_TRACE_EVENT_WINDOW_STEP = 100;

export function roundTraceDisplayText(value, fallback = "", maximum = 2_000) {
  if (typeof value === "string" && value.trim()) return value.trim().slice(0, maximum);
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  return fallback;
}

export function roundTraceErrorMessage(error, fallback = "") {
  if (error === null || error === undefined || error === "") return "";
  if (error instanceof Error && typeof error.message === "string" && error.message.trim()) {
    return error.message.trim();
  }
  if (typeof error === "string" && error.trim()) return error.trim();
  return fallback;
}

export function roundTraceDialogProjection(value) {
  if (value == null) return { present: false, ready: false, trace: null };
  const trace = normalizeRoundExecutionTrace(value);
  return {
    present: true,
    ready: trace.valid === true,
    trace,
  };
}

export function roundTraceEventWindow(events, requestedCount = ROUND_TRACE_INITIAL_EVENT_WINDOW) {
  const rows = Array.isArray(events) ? events : [];
  const numeric = Number(requestedCount);
  const count = Number.isSafeInteger(numeric) && numeric > 0
    ? Math.min(rows.length, numeric)
    : Math.min(rows.length, ROUND_TRACE_INITIAL_EVENT_WINDOW);
  const visibleRows = rows.slice(0, count);
  const hiddenCount = Math.max(0, rows.length - visibleRows.length);
  return {
    rows: visibleRows,
    totalCount: rows.length,
    visibleCount: visibleRows.length,
    hiddenCount,
    canExpand: hiddenCount > 0,
    nextCount: Math.min(rows.length, visibleRows.length + ROUND_TRACE_EVENT_WINDOW_STEP),
  };
}
