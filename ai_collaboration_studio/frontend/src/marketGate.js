const STORAGE_SYMBOLS = ["US.MU", "US.SNDK", "US.WDC", "US.STX"];
const CLOSED_SESSION_MARKET_STATES = new Set(["AFTER_HOURS_END", "CLOSED", "WAITING_OPEN"]);
const LIVE_QUOTE_MAX_AGE_SECONDS = 20 * 60;
const CLOSED_SESSION_MAX_AGE_SECONDS = 96 * 60 * 60;

function cleanSymbol(value) {
  return String(value || "").replace(/^US\./, "");
}

function securityStatusAllowsResearch(row) {
  if (!row) return true;
  if (row.suspended === true || row.suspended === 1) return false;
  const status = String(row.security_status ?? "").trim().toUpperCase();
  if (!status) return true;
  return status === "NORMAL" || status.endsWith(".NORMAL");
}

export function quoteFreshnessLabel(row) {
  if (row && !securityStatusAllowsResearch(row)) return "停牌/状态异常";
  if (!row || row.quality !== "ready") {
    return row?.quality === "future" ? "时间异常/待核验" : "过期/待核验";
  }
  if (!quoteResearchReady(row)) return "新鲜度待核验";
  if (row.quote_is_live === true) return "实时截面";
  if (
    row.quote_is_live === false
    && row.freshness_basis === "closed_session_latest_snapshot"
  ) {
    return "最近闭市截面";
  }
  return "新鲜度待核验";
}

export function quoteResearchReady(row) {
  if (!row || row.quality !== "ready" || !Number.isInteger(row.age_seconds)) return false;
  if (!securityStatusAllowsResearch(row)) return false;
  if (row.quote_is_live === true) {
    return row.freshness_basis === "live_20m_window"
      && row.age_seconds >= 0
      && row.age_seconds <= LIVE_QUOTE_MAX_AGE_SECONDS;
  }
  if (row.quote_is_live === false) {
    return row.freshness_basis === "closed_session_latest_snapshot"
      && CLOSED_SESSION_MARKET_STATES.has(String(row.market_state || "").trim().toUpperCase())
      && row.age_seconds > LIVE_QUOTE_MAX_AGE_SECONDS
      && row.age_seconds <= CLOSED_SESSION_MAX_AGE_SECONDS;
  }
  return false;
}

export function deriveMarketGate({ required, snapshot, loading }) {
  if (!required) {
    return {
      required: false,
      ready: true,
      state: "not_required",
      label: "当前房间无需行情",
      shortLabel: "无需行情",
      reason: "",
      snapshotId: "",
      capturedAt: "",
    };
  }

  if (loading) {
    return {
      required: true,
      ready: false,
      state: "checking",
      label: "正在检查四股行情",
      shortLabel: "行情检查中",
      reason: "正在检查 Futu OpenD 与 MU、SNDK、WDC、STX，请稍候。",
      snapshotId: "",
      capturedAt: "",
    };
  }

  const payload = snapshot && typeof snapshot === "object" ? snapshot : {};
  const rows = Array.isArray(payload.rows) ? payload.rows.filter((row) => row && typeof row === "object") : [];
  const readySymbols = new Set(rows.flatMap((row) => {
    const last = Number(row.last);
    const ready = quoteResearchReady(row)
      && Number.isFinite(last)
      && last > 0
      && Boolean(String(row.market_time || "").trim());
    return ready ? [String(row.symbol || "")] : [];
  }));
  const missingSymbols = STORAGE_SYMBOLS.filter((symbol) => !readySymbols.has(symbol));
  const sourceErrors = Array.isArray(payload.source_errors) ? payload.source_errors : [];
  const declaredMissing = Array.isArray(payload.missing_symbols) ? payload.missing_symbols : [];
  const securityBlockedSymbols = rows
    .filter((row) => !securityStatusAllowsResearch(row))
    .map((row) => String(row.symbol || ""))
    .filter(Boolean);
  const safetyReady = payload.execution_capability === "none" && payload.live_trading_allowed === false;
  const ready = payload.source === "futu_opend"
    && payload.ok === true
    && payload.state === "ready"
    && Boolean(String(payload.snapshot_id || "").trim())
    && Boolean(String(payload.captured_at || "").trim())
    && sourceErrors.length === 0
    && declaredMissing.length === 0
    && missingSymbols.length === 0
    && safetyReady;

  if (ready) {
    return {
      required: true,
      ready: true,
      state: "ready",
      label: "Futu 四股行情已就绪",
      shortLabel: "行情已就绪",
      reason: "",
      snapshotId: String(payload.snapshot_id),
      capturedAt: String(payload.captured_at),
      readyCount: STORAGE_SYMBOLS.length,
      missingSymbols: [],
    };
  }

  const firstError = sourceErrors.find((item) => item && typeof item === "object") || {};
  let reason = "";
  let shortLabel = "行情未就绪";
  if (!safetyReady && Object.keys(payload).length) {
    shortLabel = "只读边界异常";
    reason = "不能开始新一轮：行情快照没有明确保持只读边界。";
  } else if (firstError.code === "FUTU_OPEND_OFFLINE" || payload.state === "offline") {
    shortLabel = "富途离线";
    reason = `不能开始新一轮：${firstError.message || "本机 Futu OpenD 未连接"}（127.0.0.1:11111）。`;
  } else if (securityBlockedSymbols.length) {
    shortLabel = "停牌/状态异常";
    reason = `不能开始新一轮：证券停牌或状态异常 ${securityBlockedSymbols.map(cleanSymbol).join("、")}。`;
  } else if (missingSymbols.length || declaredMissing.length) {
    const symbols = [...new Set([...missingSymbols, ...declaredMissing].map(cleanSymbol).filter(Boolean))];
    shortLabel = "行情不完整";
    reason = `不能开始新一轮：缺少或未通过核验的行情 ${symbols.join("、") || "未知"}。`;
  } else if (firstError.message) {
    reason = `不能开始新一轮：${firstError.message}`;
  } else if (!Object.keys(payload).length) {
    reason = "尚未取得 Futu 四股只读行情，请刷新后再开始新一轮。";
  } else {
    reason = "不能开始新一轮：Futu 四股行情快照不完整或质量不足。";
  }

  return {
    required: true,
    ready: false,
    state: String(payload.state || "unknown"),
    label: shortLabel,
    shortLabel,
    reason,
    code: String(firstError.code || ""),
    severity: !safetyReady && Object.keys(payload).length ? "critical" : "attention",
    snapshotId: String(payload.snapshot_id || ""),
    capturedAt: String(payload.captured_at || ""),
    readyCount: readySymbols.size,
    missingSymbols,
  };
}
