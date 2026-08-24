import {
  AlertTriangle,
  ChevronDown,
  Database,
  FileJson,
  LoaderCircle,
  Search,
  ShieldCheck,
  Upload,
} from "lucide-react";
import { memo, useEffect, useId, useMemo, useRef, useState } from "react";
import "../styles/stock-research.css";
import { api } from "../api";
import {
  STOCK_EVIDENCE_CLASSES,
  STOCK_PREFLIGHT_SOURCE_TYPES,
  buildStockRoundContextAuthorization,
  normalizeStockResearchResponse,
  parseStockResearchJson,
  stockRoundContextAuthorizationState,
} from "../stockResearch.js";

const MAX_JSON_BYTES = 1_000_000;
const SOURCE_LABELS = Object.freeze({
  futu: "Futu 快照",
  sec: "SEC 披露",
  investor_relations: "公司 IR",
  price_adjustment: "复权口径",
  corporate_actions: "公司行动",
});
const EVIDENCE_LABELS = Object.freeze({
  official_fact: "官方事实",
  media_report: "媒体信息",
  model_inference: "模型推断",
  market_proxy: "市场代理",
});

function initialRequestState() {
  return { status: "idle", view: null, error: "" };
}


function requestErrorMessage(error, fallback) {
  const message = typeof error?.message === "string" ? error.message.trim().slice(0, 1000) : "";
  return message || fallback;
}


const StockPreflightCard = memo(function StockPreflightCard({ stock, summary }) {
  return (
    <article className={summary?.research_ready ? "stock-symbol-card ready" : "stock-symbol-card blocked"} role="listitem">
      <header>
        <span><strong>{stock.symbol}</strong><small>{stock.issuer_name}</small></span>
        <em>{summary?.research_ready ? "五项已核验" : "存在缺口"}</em>
      </header>
      <p>{stock.exchange} · {stock.currency}</p>
      <div className="stock-preflight-grid" role="list" aria-label={`${stock.symbol} 五项来源预检`}>
        {STOCK_PREFLIGHT_SOURCE_TYPES.map((sourceType) => {
          const state = summary?.[sourceType] || {};
          const source = stock.preflight?.[sourceType]?.source || null;
          return (
            <section className={state.status === "ready" ? "ready" : "unavailable"} key={sourceType} role="listitem">
              <span><strong>{SOURCE_LABELS[sourceType]}</strong><em>{state.status === "ready" ? "已核验" : "不可用"}</em></span>
              <small>截止：{state.as_of_utc || "未提供"}</small>
              <small>来源：{source?.publisher || "未绑定"}</small>
              {state.reason ? <p>{state.reason}</p> : null}
            </section>
          );
        })}
      </div>
    </article>
  );
});

const StockEvidenceGrid = memo(function StockEvidenceGrid({ claims }) {
  const claimsByEvidenceClass = useMemo(() => {
    const grouped = new Map(
      STOCK_EVIDENCE_CLASSES.map((evidenceClass) => [evidenceClass, []]),
    );
    for (const claim of claims) grouped.get(claim.evidenceClass)?.push(claim);
    return grouped;
  }, [claims]);

  return (
    <section className="stock-evidence-section" aria-label="股票研究证据分类">
      <div className="stock-section-heading"><strong>四类证据严格分层</strong><small>不把媒体、推断或市场代理冒充官方事实</small></div>
      <div className="stock-evidence-grid" role="list">
        {STOCK_EVIDENCE_CLASSES.map((evidenceClass) => {
          const rows = claimsByEvidenceClass.get(evidenceClass);
          return (
            <article key={evidenceClass} role="listitem">
              <header><strong>{EVIDENCE_LABELS[evidenceClass]}</strong><em>{rows.length}</em></header>
              {rows.length ? rows.map((claim, index) => (
                <div className="stock-evidence-claim" key={JSON.stringify([claim.symbol, claim.claimId, index])}>
                  <span><b>{claim.symbol}</b><small>{claim.asOfUtc || "无截止时间"}</small></span>
                  <p>{claim.claim}</p>
                  <small>{claim.publisher || "来源未命名"} · {claim.materialId || "无材料"} v{claim.materialVersion || "?"}</small>
                </div>
              )) : <p className="stock-empty-copy">本合同没有此类证据，不做补写。</p>}
            </article>
          );
        })}
      </div>
    </section>
  );
});

export const StockResearchPanel = memo(function StockResearchPanel({
  room,
  activation,
  roundContextAuthorization = null,
  onRoundContextAuthorizationChange,
}) {
  const [expanded, setExpanded] = useState(false);
  const [source, setSource] = useState("");
  const [requestState, setRequestState] = useState(initialRequestState);
  const requestRef = useRef(null);
  const inspectionGenerationRef = useRef(0);
  const fileReadGenerationRef = useRef(0);
  const panelBodyId = useId();
  const panelTitleId = useId();
  const roomId = typeof room?.id === "string" ? room.id.trim().slice(0, 240) : "";

  const invalidateInspection = () => {
    inspectionGenerationRef.current += 1;
    requestRef.current?.abort();
    requestRef.current = null;
  };

  useEffect(() => () => {
    fileReadGenerationRef.current += 1;
    inspectionGenerationRef.current += 1;
    requestRef.current?.abort();
    requestRef.current = null;
  }, []);
  useEffect(() => {
    fileReadGenerationRef.current += 1;
    inspectionGenerationRef.current += 1;
    requestRef.current?.abort();
    requestRef.current = null;
    setExpanded(false);
    setSource("");
    setRequestState(initialRequestState());
  }, [roomId]);

  if (!activation?.visible) return null;
  const active = activation.active === true;
  const canUpdateRoundAuthorization = typeof onRoundContextAuthorizationChange === "function";
  const updateSource = (value) => {
    fileReadGenerationRef.current += 1;
    invalidateInspection();
    if (roundContextAuthorization && canUpdateRoundAuthorization) onRoundContextAuthorizationChange(null);
    setSource(value);
    setRequestState(initialRequestState());
  };
  const importJson = async (event) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    const fileReadGeneration = ++fileReadGenerationRef.current;
    invalidateInspection();
    if (file.size > MAX_JSON_BYTES) {
      setRequestState({ status: "error", view: null, error: "JSON 文件超过 1 MB 前端上限。" });
      return;
    }
    try {
      const nextSource = await file.text();
      if (fileReadGenerationRef.current !== fileReadGeneration) return;
      updateSource(nextSource);
    } catch {
      if (fileReadGenerationRef.current !== fileReadGeneration) return;
      setRequestState({ status: "error", view: null, error: "无法在本地读取这个 JSON 文件。" });
    }
  };
  const inspect = async () => {
    if (!active || !roomId || requestState.status === "loading") return;
    if (roundContextAuthorization && canUpdateRoundAuthorization) onRoundContextAuthorizationChange(null);
    if (new Blob([source]).size > MAX_JSON_BYTES) {
      setRequestState({ status: "error", view: null, error: "JSON 文本超过 1 MB 前端上限。" });
      return;
    }
    let payload;
    try {
      payload = parseStockResearchJson(source);
    } catch (error) {
      setRequestState({ status: "error", view: null, error: requestErrorMessage(error, "JSON 合同无法解析。") });
      return;
    }
    invalidateInspection();
    const inspectionGeneration = inspectionGenerationRef.current;
    const controller = new AbortController();
    requestRef.current = controller;
    setRequestState({ status: "loading", view: null, error: "" });
    try {
      const response = await api.inspectStockResearch(roomId, payload, controller.signal);
      if (inspectionGenerationRef.current !== inspectionGeneration) return;
      const view = normalizeStockResearchResponse(response, {
        roomId,
        stockRoomScope: room?.stock_room_scope,
      });
      setRequestState(view.valid
        ? { status: "ready", view, error: "" }
        : { status: "integrity_failed", view: null, error: view.reason });
    } catch (error) {
      if (inspectionGenerationRef.current !== inspectionGeneration || error?.name === "AbortError") return;
      setRequestState({ status: "error", view: null, error: requestErrorMessage(error, "股票只读检查失败。") });
    } finally {
      if (requestRef.current === controller) requestRef.current = null;
    }
  };

  const view = requestState.view;
  const requestAnnouncement = requestState.status === "loading"
    ? "正在核验股票研究合同。"
    : requestState.status === "ready"
      ? "股票研究合同核验完成。"
      : "";
  const authorizationState = stockRoundContextAuthorizationState(
    roundContextAuthorization,
    {
      roomId,
      contractSha256: view?.contract?.contract_sha256,
      stockRoomScopeSha256: room?.stock_room_scope_sha256,
      pluginRegistrySnapshotSha256: activation.slot?.snapshotSha256,
    },
  );
  const authorizeForRound = () => {
    if (!view?.valid || !canUpdateRoundAuthorization) return;
    try {
      onRoundContextAuthorizationChange(
        buildStockRoundContextAuthorization(view, activation, room),
      );
    } catch (error) {
      setRequestState({ status: "error", view: null, error: requestErrorMessage(error, "无法构造下一轮冻结上下文。") });
    }
  };
  const preflightBySymbol = new Map(
    (view?.raw?.symbol_preflights || []).map((item) => [item.symbol, item]),
  );

  return (
    <section className={expanded ? "stock-research-panel expanded" : "stock-research-panel"} aria-labelledby={panelTitleId} aria-busy={requestState.status === "loading"}>
      <button className="stock-research-toggle" type="button" onClick={() => setExpanded((value) => !value)} aria-expanded={expanded} aria-controls={panelBodyId}>
        <span><Database size={16} aria-hidden="true" /><strong id={panelTitleId}>通用股票只读证据</strong><small>{active ? "精确 v2 贡献可用" : "仅显示冻结边界"}</small></span>
        <ChevronDown size={16} aria-hidden="true" />
      </button>
      {requestAnnouncement ? <p className="stock-request-status" role="status" aria-live="polite" aria-atomic="true">{requestAnnouncement}</p> : null}
      {expanded ? (
        <div className="stock-research-body" id={panelBodyId} aria-busy={requestState.status === "loading"}>
          <p className="stock-scope-copy">
            房间股票池：{room?.stock_room_scope?.symbols?.join(" · ") || "未冻结"}
          </p>
          {!active ? <p className="stock-research-warning" role="alert"><AlertTriangle size={15} aria-hidden="true" />{activation.reason}</p> : (
            <>
              <label className="stock-json-input">
                <span><FileJson size={14} aria-hidden="true" />股票研究合同 JSON</span>
                <textarea value={source} maxLength={MAX_JSON_BYTES} onChange={(event) => updateSource(event.target.value)} placeholder="粘贴与当前房间股票池完全一致的离线合同 JSON" spellCheck={false} autoCapitalize="off" autoCorrect="off" />
              </label>
              <div className="stock-json-actions">
                <label className="secondary compact stock-import-button">
                  <Upload size={14} aria-hidden="true" />从本机导入 JSON
                  <input type="file" accept=".json,application/json" onChange={importJson} />
                </label>
                <button className="primary compact" type="button" onClick={inspect} disabled={requestState.status === "loading" || !source.trim()}>
                  {requestState.status === "loading" ? <LoaderCircle className="spin" size={14} aria-hidden="true" /> : <Search size={14} aria-hidden="true" />}
                  {requestState.status === "loading" ? "核验封印中…" : "执行只读检查"}
                </button>
              </div>
            </>
          )}
          {requestState.error ? <p className="stock-research-error" role="alert"><AlertTriangle size={14} aria-hidden="true" />{requestState.error}</p> : null}

          {view?.valid ? (
            <div className="stock-research-result">
              <section className={authorizationState.valid ? "stock-round-context authorized" : "stock-round-context"}>
                <span>
                  <strong>{authorizationState.valid ? "已显式加入下一轮冻结上下文" : "下一轮上下文尚未授权"}</strong>
                  <small>数据截止 {view.contract.data_cutoff_utc} · 合同 {view.contract.contract_sha256.slice(0, 10)}…</small>
                </span>
                <button className="secondary compact" type="button" disabled={!canUpdateRoundAuthorization} onClick={authorizationState.valid ? () => canUpdateRoundAuthorization && onRoundContextAuthorizationChange(null) : authorizeForRound}>
                  {authorizationState.valid ? "撤销下一轮授权" : "显式用于下一轮"}
                </button>
                <p>只冻结本次已核验合同、房间股票池及哈希；不会自动开始、调用真实 Provider、交易或替代用户决定。</p>
              </section>
              <div className="stock-section-heading"><strong>逐标的五项预检</strong><small>{view.raw.research_ready ? "五项预检均已核验" : "存在明确不可用项"} · 截止 {view.raw.data_cutoff_utc}</small></div>
              <div className="stock-symbol-grid" role="list" aria-label="逐标的五项预检">
                {view.contract.symbols.map((stock) => <StockPreflightCard stock={stock} summary={preflightBySymbol.get(stock.symbol)} key={stock.symbol} />)}
              </div>
              <StockEvidenceGrid claims={view.claims} />
              <section className="stock-safety-boundary" aria-label="股票只读安全边界">
                <ShieldCheck size={17} aria-hidden="true" />
                <span><strong>研究只读，用户最终决定</strong><small>execution_capability=none · live_trading=false · order/wallet/automatic_trading=false · provider/market/business writes=0</small></span>
              </section>
            </div>
          ) : null}
        </div>
      ) : null}
    </section>
  );
});
