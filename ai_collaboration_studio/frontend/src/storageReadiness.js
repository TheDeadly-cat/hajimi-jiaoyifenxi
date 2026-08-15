const SOURCE_LABELS = {
  futu_sdk: "Futu OpenAPI SDK",
  futu_opend: "Futu OpenD 四股行情",
  sec_edgar: "SEC EDGAR 官方申报",
  company_ir: "公司 IR 官方事件",
  earnings_materials: "官方业绩材料包",
  industry_proxies: "FRED 行业供需代理",
};

const SOURCE_GROUPS = {
  futu_sdk: "round_admission",
  futu_opend: "round_admission",
  sec_edgar: "convergence",
  company_ir: "convergence",
  earnings_materials: "convergence",
  industry_proxies: "convergence",
};

const STORAGE_SYMBOLS = new Set(["US.MU", "US.SNDK", "US.WDC", "US.STX"]);

export function deriveOfficialSupplementCandidates(readiness) {
  const rows = readiness?.independent_evidence?.evidence?.official_earnings_materials?.rows;
  if (!Array.isArray(rows)) return [];

  const candidates = [];
  const seen = new Set();
  for (const row of rows) {
    const rowSymbol = String(row?.symbol || "").trim().toUpperCase();
    if (!STORAGE_SYMBOLS.has(rowSymbol)) continue;
    const rejected = Array.isArray(row?.rejected_curated_materials)
      ? row.rejected_curated_materials
      : [];
    for (const material of rejected) {
      const symbol = String(material?.symbol || rowSymbol).trim().toUpperCase();
      const officialUrl = String(material?.official_url || "").trim();
      const fiscalPeriod = String(material?.fiscal_period || "").trim();
      const materialKind = String(material?.material_kind || "").trim();
      const errorCodes = [...new Set((row?.source_errors || [])
        .map((error) => String(error?.code || "").trim())
        .filter((code) => [
          "EARNINGS_MATERIAL_ACCESS_TIMEOUT",
          "EARNINGS_MATERIAL_ACCESS_ERROR",
        ].includes(code)))];
      if (
        symbol !== rowSymbol
        || !STORAGE_SYMBOLS.has(symbol)
        || !officialUrl.startsWith("https://")
        || !fiscalPeriod
        || !materialKind
        || !errorCodes.length
      ) continue;
      const id = `${symbol}:${fiscalPeriod}:${materialKind}:${officialUrl}`;
      if (seen.has(id)) continue;
      seen.add(id);
      candidates.push({
        id,
        symbol,
        title: String(material?.title || `${symbol.replace("US.", "")} ${fiscalPeriod} 官方业绩材料`).trim(),
        publisher: String(row?.publisher || "公司 Investor Relations").trim(),
        official_url: officialUrl,
        fiscal_period: fiscalPeriod,
        material_kind: materialKind,
        error_codes: errorCodes,
      });
    }
  }
  return candidates;
}

function initialSources(status = {}) {
  const sec = status.sec_edgar || {};
  return [
    {
      id: "futu_sdk",
      label: SOURCE_LABELS.futu_sdk,
      group: "round_admission",
      state: status.sdk_available ? "ready" : "blocked",
      ready: status.sdk_available === true,
      action: "安装项目 requirements.txt 中的 futu-api，并重启本地服务。",
    },
    {
      id: "futu_opend",
      label: SOURCE_LABELS.futu_opend,
      group: "round_admission",
      state: status.opend_reachable ? "checking" : "blocked",
      ready: false,
      coverage_ready: null,
      coverage_total: 4,
      action: `安装或启动并登录 Futu OpenD，保持只读行情端口 ${status.host || "127.0.0.1"}:${status.port || 11111}。`,
    },
    {
      id: "sec_edgar",
      label: SOURCE_LABELS.sec_edgar,
      group: "convergence",
      state: sec.configured ? "unchecked" : "blocked",
      ready: false,
      coverage_ready: null,
      coverage_total: 4,
      action: sec.configured
        ? "点击刷新官方资料，核验四股 SEC 覆盖。"
        : "在本机 .env.local 配置 SEC_USER_AGENT=产品或组织名 联系邮箱，然后重启服务。",
    },
    ...["company_ir", "earnings_materials", "industry_proxies"].map((id) => ({
      id,
      label: SOURCE_LABELS[id],
      group: "convergence",
      state: "unchecked",
      ready: false,
      coverage_ready: null,
      coverage_total: id === "industry_proxies" ? null : 4,
      action: "点击刷新官方资料进行真实拉取核验。",
    })),
  ];
}

export function deriveStorageReadinessView(status, readiness, marketGate = null) {
  const suppliedSources = Array.isArray(readiness?.sources) && readiness.sources.length
    ? readiness.sources.map((source) => ({
      ...source,
      label: source.label || SOURCE_LABELS[source.id] || source.id,
      group: source.group || SOURCE_GROUPS[source.id] || "",
    }))
    : initialSources(status || {});
  const suppliedRoundAdmission = readiness?.round_admission || {
    ready: false,
    state: "blocked",
    coverage_ready: status?.opend_reachable ? null : 0,
    coverage_total: 4,
    reason: status?.opend_reachable
      ? "需要刷新并核验 MU、SNDK、WDC、STX 四股共同截面。"
      : "本机 Futu OpenD 未连接。",
  };
  const marketGateRequired = marketGate?.required === true;
  const marketGateReady = marketGateRequired && marketGate?.ready === true;
  const marketGateChecking = marketGateRequired && marketGate?.state === "checking";
  const sources = marketGateRequired
    ? suppliedSources.map((source) => {
      if (source.id !== "futu_opend") return source;
      const readyCount = Number.isInteger(marketGate?.readyCount)
        ? marketGate.readyCount
        : marketGateReady ? 4 : 0;
      return {
        ...source,
        state: marketGateReady ? "ready" : marketGateChecking ? "checking" : "blocked",
        ready: marketGateReady,
        coverage_ready: readyCount,
        coverage_total: 4,
        error_codes: marketGateReady ? [] : source.error_codes,
        action: marketGateReady
          ? "当前新轮四股准入快照已通过；刷新按钮只继续核验 SEC、IR、业绩材料和行业代理。"
          : source.action,
      };
    })
    : suppliedSources;
  const roundAdmission = marketGateRequired
    ? {
      ...suppliedRoundAdmission,
      ready: marketGateReady,
      state: marketGateReady ? "ready" : marketGateChecking ? "checking" : "blocked",
      coverage_ready: Number.isInteger(marketGate?.readyCount)
        ? marketGate.readyCount
        : marketGateReady ? 4 : 0,
      coverage_total: 4,
      reason_code: marketGateReady ? "READY" : marketGate?.code || suppliedRoundAdmission.reason_code,
      reason: marketGateReady ? "" : marketGate?.reason || suppliedRoundAdmission.reason,
      snapshot_id: marketGateReady ? marketGate?.snapshotId || "" : "",
      captured_at: marketGateReady ? marketGate?.capturedAt || "" : "",
    }
    : suppliedRoundAdmission;
  const suppliedConvergence = readiness?.convergence_readiness || {};
  const suppliedBlockers = Array.isArray(suppliedConvergence.blockers)
    ? suppliedConvergence.blockers
    : [];
  const suppliedBlockersBySource = new Map(
    suppliedBlockers
      .filter((blocker) => blocker?.source_id)
      .map((blocker) => [blocker.source_id, blocker]),
  );
  const blockers = sources
    .filter((source) => ["round_admission", "convergence"].includes(source.group) && source.ready !== true)
    .map((source) => {
      const supplied = suppliedBlockersBySource.get(source.id) || {};
      suppliedBlockersBySource.delete(source.id);
      return {
        ...supplied,
        source_id: source.id,
        label: supplied.label || source.label,
        action: supplied.action || source.action,
        error_codes: Array.isArray(supplied.error_codes)
          ? supplied.error_codes
          : Array.isArray(source.error_codes) ? source.error_codes : [],
      };
    });
  blockers.push(...suppliedBlockersBySource.values());
  const convergence = {
    ...suppliedConvergence,
    ready: false,
    state: suppliedConvergence.state || "unchecked",
    preparation_usable: suppliedConvergence.preparation_usable === true,
    blockers,
  };
  convergence.ready = suppliedConvergence.ready === true && blockers.length === 0;
  const checked = Boolean(readiness?.version);
  const readyCount = sources.filter((source) => source.ready).length;
  const partialCount = sources.filter((source) => source.state === "partial").length;
  return {
    checked,
    sources,
    roundAdmission,
    convergence,
    readyCount,
    partialCount,
    totalCount: sources.length,
    safetyReady: readiness?.safety?.ready === true,
  };
}

export function storageSourceStateLabel(source) {
  if (source?.state === "ready") return "已就绪";
  if (source?.state === "ready_with_manual_substitution") return "人工核验副本";
  if (source?.state === "partial") return "部分可用";
  if (source?.state === "blocked") return "未就绪";
  if (source?.state === "checking") return "待核验";
  return "未拉取";
}

export function storageCoverageText(source) {
  const ready = source?.coverage_ready;
  const total = source?.coverage_total;
  if (ready === null || ready === undefined || ready === ""
    || total === null || total === undefined || total === "") {
    return "—";
  }
  const readyNumber = Number(ready);
  const totalNumber = Number(total);
  if (!Number.isFinite(readyNumber) || !Number.isFinite(totalNumber)) return "—";
  return `${readyNumber}/${totalNumber}`;
}

function hasUsableRows(value) {
  if (!value || typeof value !== "object") return false;
  if (Array.isArray(value.rows) && value.rows.length) return true;
  if (Array.isArray(value.derived) && value.derived.length) return true;
  return false;
}

export function mergePreparedResearchEvidence(liveEvidence, independentEvidence) {
  const live = liveEvidence && typeof liveEvidence === "object" ? liveEvidence : {};
  const prepared = independentEvidence && typeof independentEvidence === "object"
    ? independentEvidence
    : {};
  const merged = { ...live };
  for (const key of [
    "official_filings",
    "company_ir_releases",
    "official_earnings_packs",
    "official_earnings_materials",
    "industry_supply_demand",
  ]) {
    const current = live[key];
    const candidate = prepared[key];
    if (!candidate) continue;
    const currentState = String(current?.state || "").toLowerCase();
    if (!current || ["skipped", "offline", "empty"].includes(currentState) || (!hasUsableRows(current) && hasUsableRows(candidate))) {
      merged[key] = candidate;
    }
  }
  return merged;
}
