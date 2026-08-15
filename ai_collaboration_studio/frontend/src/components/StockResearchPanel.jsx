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
import { useEffect, useRef, useState } from "react";
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

function StockPreflightCard({ stock, summary }) {
  return (
    <article className={summary?.research_ready ? "stock-symbol-card ready" : "stock-symbol-card blocked"}>
      <header>
        <span><strong>{stock.symbol}</strong><small>{stock.issuer_name}</small></span>
        <em>{summary?.research_ready ? "五项齐备" : "存在缺口"}</em>
      </header>
      <p>{stock.exchange} · {stock.currency}</p>
      <div className="stock-preflight-grid">
        {STOCK_PREFLIGHT_SOURCE_TYPES.map((sourceType) => {
          const state = summary?.[sourceType] || {};
          const source = stock.preflight?.[sourceType]?.source || null;
          return (
            <section className={state.status === "ready" ? "ready" : "unavailable"} key={sourceType}>
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
}

function StockEvidenceGrid({ claims }) {
  return (
    <section className="stock-evidence-section" aria-label="股票研究证据分类">
      <div className="stock-section-heading"><strong>四类证据严格分层</strong><small>不把媒体、推断或市场代理冒充官方事实</small></div>
      <div className="stock-evidence-grid">
        {STOCK_EVIDENCE_CLASSES.map((evidenceClass) => {
          const rows = claims.filter((claim) => claim.evidenceClass === evidenceClass);
          return (
            <article key={evidenceClass}>
              <header><strong>{EVIDENCE_LABELS[evidenceClass]}</strong><em>{rows.length}</em></header>
              {rows.length ? rows.map((claim) => (
                <div className="stock-evidence-claim" key={`${claim.symbol}:${claim.claimId}`}>
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
}

export function StockResearchPanel({
  room,
  activation,
  roundContextAuthorization = null,
  onRoundContextAuthorizationChange,
}) {
  const [expanded, setExpanded] = useState(false);
  const [source, setSource] = useState("");
  const [requestState, setRequestState] = useState(initialRequestState);
  const requestRef = useRef(null);
  const roomId = String(room?.id || "");

  useEffect(() => () => requestRef.current?.abort(), []);
  useEffect(() => {
    requestRef.current?.abort();
    setExpanded(false);
    setSource("");
    setRequestState(initialRequestState());
  }, [roomId]);

  if (!activation?.visible) return null;
  const active = activation.active === true;
  const updateSource = (value) => {
    if (roundContextAuthorization) onRoundContextAuthorizationChange?.(null);
    setSource(value);
    setRequestState(initialRequestState());
  };
  const importJson = async (event) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    if (file.size > MAX_JSON_BYTES) {
      setRequestState({ status: "error", view: null, error: "JSON 文件超过 1 MB 前端上限。" });
      return;
    }
    try {
      updateSource(await file.text());
    } catch {
      setRequestState({ status: "error", view: null, error: "无法在本地读取这个 JSON 文件。" });
    }
  };
  const inspect = async () => {
    if (!active || !roomId || requestState.status === "loading") return;
    if (roundContextAuthorization) onRoundContextAuthorizationChange?.(null);
    let payload;
    try {
      payload = parseStockResearchJson(source);
    } catch (error) {
      setRequestState({ status: "error", view: null, error: error.message });
      return;
    }
    requestRef.current?.abort();
    const controller = new AbortController();
    requestRef.current = controller;
    setRequestState({ status: "loading", view: null, error: "" });
    try {
      const response = await api.inspectStockResearch(roomId, payload, controller.signal);
      const view = normalizeStockResearchResponse(response, {
        roomId,
        stockRoomScope: room?.stock_room_scope,
      });
      setRequestState(view.valid
        ? { status: "ready", view, error: "" }
        : { status: "integrity_failed", view: null, error: view.reason });
    } catch (error) {
      if (error?.name === "AbortError") return;
      setRequestState({ status: "error", view: null, error: error.message || "股票只读检查失败。" });
    } finally {
      if (requestRef.current === controller) requestRef.current = null;
    }
  };

  const view = requestState.view;
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
    if (!view?.valid) return;
    try {
      onRoundContextAuthorizationChange?.(
        buildStockRoundContextAuthorization(view, activation, room),
      );
    } catch (error) {
      setRequestState({ status: "error", view: null, error: error.message });
    }
  };
  const preflightBySymbol = new Map(
    (view?.raw?.symbol_preflights || []).map((item) => [item.symbol, item]),
  );

  return (
    <section className={expanded ? "stock-research-panel expanded" : "stock-research-panel"} aria-label="通用股票只读研究检查器">
      <button className="stock-research-toggle" type="button" onClick={() => setExpanded((value) => !value)} aria-expanded={expanded}>
        <span><Database size={16} /><strong>通用股票只读证据</strong><small>{active ? "精确 v2 贡献可用" : "仅显示冻结边界"}</small></span>
        <ChevronDown size={16} />
      </button>
      {expanded ? (
        <div className="stock-research-body">
          <p className="stock-scope-copy">
            房间股票池：{room?.stock_room_scope?.symbols?.join(" · ") || "未冻结"}
          </p>
          {!active ? <p className="stock-research-warning" role="alert"><AlertTriangle size={15} />{activation.reason}</p> : (
            <>
              <label className="stock-json-input">
                <span><FileJson size={14} />股票研究合同 JSON</span>
                <textarea value={source} onChange={(event) => updateSource(event.target.value)} placeholder="粘贴与当前房间股票池完全一致的离线合同 JSON" />
              </label>
              <div className="stock-json-actions">
                <label className="secondary compact stock-import-button">
                  <Upload size={14} />从本机导入 JSON
                  <input type="file" accept=".json,application/json" onChange={importJson} />
                </label>
                <button className="primary compact" type="button" onClick={inspect} disabled={requestState.status === "loading" || !source.trim()}>
                  {requestState.status === "loading" ? <LoaderCircle className="spin" size={14} /> : <Search size={14} />}
                  {requestState.status === "loading" ? "核验封印中…" : "执行只读检查"}
                </button>
              </div>
            </>
          )}
          {requestState.error ? <p className="stock-research-error" role="alert"><AlertTriangle size={14} />{requestState.error}</p> : null}

          {view?.valid ? (
            <div className="stock-research-result">
              <section className={authorizationState.valid ? "stock-round-context authorized" : "stock-round-context"}>
                <span>
                  <strong>{authorizationState.valid ? "已显式加入下一轮冻结上下文" : "下一轮上下文尚未授权"}</strong>
                  <small>数据截止 {view.contract.data_cutoff_utc} · 合同 {view.contract.contract_sha256.slice(0, 10)}…</small>
                </span>
                <button className="secondary compact" type="button" onClick={authorizationState.valid ? () => onRoundContextAuthorizationChange?.(null) : authorizeForRound}>
                  {authorizationState.valid ? "撤销下一轮授权" : "显式用于下一轮"}
                </button>
                <p>只冻结本次已核验合同、房间股票池及哈希；不会自动开始、调用真实 Provider、交易或替代用户决定。</p>
              </section>
              <div className="stock-section-heading"><strong>逐标的五项预检</strong><small>{view.raw.research_ready ? "全部研究就绪" : "存在明确不可用项"} · 截止 {view.raw.data_cutoff_utc}</small></div>
              <div className="stock-symbol-grid">
                {view.contract.symbols.map((stock) => <StockPreflightCard stock={stock} summary={preflightBySymbol.get(stock.symbol)} key={stock.symbol} />)}
              </div>
              <StockEvidenceGrid claims={view.claims} />
              <section className="stock-safety-boundary" aria-label="股票只读安全边界">
                <ShieldCheck size={17} />
                <span><strong>研究只读，用户最终决定</strong><small>execution_capability=none · live_trading=false · order/wallet/automatic_trading=false · provider/market/business writes=0</small></span>
              </section>
            </div>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
