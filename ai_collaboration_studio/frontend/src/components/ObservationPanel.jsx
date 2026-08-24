import { BookCheck, CheckCircle2, FlaskConical, GitBranch, LoaderCircle, Plus, RefreshCcw, ShieldCheck, X } from "lucide-react";
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { CalibrationSummary } from "./CalibrationSummary";
import { useModalFocus } from "../useModalFocus";
import "../styles/observation.css";


const directionText = { UP: "看涨", DOWN: "看跌", NEUTRAL: "中性" };
const statusText = {
  PROPOSED: "待你确认",
  PENDING_BASELINE: "等待真实基准",
  OPEN: "观察中",
  RESOLVED: "已结算",
  CANCELLED: "已取消",
};

function finiteNumber(value) {
  if (value === null || value === undefined || value === "" || typeof value === "boolean") return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function resultSuffix(item) {
  if (item.status !== "RESOLVED") return "";
  const relative = finiteNumber(item.relative_return_pct);
  const absolute = finiteNumber(item.return_pct);
  const relativeText = relative !== null
    ? ` · 同行差 ${relative > 0 ? "+" : ""}${relative.toFixed(2)}%`
    : "";
  const outcomeText = item.hit === true ? "命中" : item.hit === false ? "未命中" : "命中状态未返回";
  const returnText = absolute === null ? "收益未返回" : `${absolute.toFixed(2)}%`;
  return ` · ${outcomeText} · ${returnText}${relativeText}`;
}


export function ObservationPanel({ observations, reflections, scorecard, loading, onAdd, onConfirm, onReconcile, onEditReflection }) {
  const [showAll, setShowAll] = useState(false);
  const observationItems = useMemo(() => observations || [], [observations]);
  const visible = useMemo(
    () => (showAll ? observationItems : observationItems.slice(0, 6)),
    [observationItems, showAll],
  );
  const { reflectionByObservation, confirmedReflections } = useMemo(() => {
    const byObservation = new Map();
    let confirmedCount = 0;
    for (const reflection of reflections || []) {
      byObservation.set(reflection.observation_id, reflection);
      if (reflection.status === "CONFIRMED") confirmedCount += 1;
    }
    return {
      reflectionByObservation: byObservation,
      confirmedReflections: confirmedCount,
    };
  }, [reflections]);
  return (
    <section className="observation-panel" aria-label="模拟观察与验证">
      <CalibrationSummary scorecard={scorecard} />
      <p className="observation-boundary"><ShieldCheck aria-hidden="true" size={14} />AI 主观置信度不等于统计胜率；仅用户确认后的真实到期样本计分。已确认 {confirmedReflections} 条反思记忆。</p>
      <div className="observation-actions">
        <button className="secondary compact" type="button" onClick={(event) => onAdd(event.currentTarget)}><Plus aria-hidden="true" size={13} />新建观察</button>
        <button className="secondary compact" type="button" onClick={onReconcile} disabled={loading} aria-busy={loading}>
          {loading ? <LoaderCircle aria-hidden="true" className="spin" size={13} /> : <RefreshCcw aria-hidden="true" size={13} />}刷新验证
        </button>
      </div>
      {loading ? <p className="observation-request-status" role="status" aria-live="polite">正在刷新真实到期样本与验证状态。</p> : null}
      {visible.length ? <div className="observation-list">
        {visible.map((item) => {
          const reflection = reflectionByObservation.get(item.id);
          const statusClass = statusText[item.status] ? String(item.status).toLowerCase() : "unknown";
          return <article className={`observation-row ${statusClass}`} key={item.id}>
            <div>
              <strong>{String(item.symbol || "未知标的").replace("US.", "")} · {directionText[item.direction] || "方向未返回"} · {item.horizon_days || "?"}日</strong>
              <small>{item.created_by === "user" ? "用户记录" : "AI 提案"} · {item.methodology_id || "directional_threshold"}@v{item.methodology_version || 1} · {statusText[item.status] || "状态未知"}{resultSuffix(item)}</small>
              {item.status === "PROPOSED" ? <details className="observation-confirm-details">
                <summary>查看完整依据后确认</summary>
                <dl>
                  <div><dt>命中阈值</dt><dd>{Number(item.threshold_pct || 0).toFixed(2)}%</dd></div>
                  <div><dt>依据</dt><dd>{item.thesis}</dd></div>
                  <div><dt>反证</dt><dd>{item.counter_case || "未提供"}</dd></div>
                  <div><dt>置信度</dt><dd>{item.model_confidence === null || item.model_confidence === undefined ? "未提供" : `${item.model_confidence}%（${item.confidence_source === "ai" ? "AI" : "用户"}）`}</dd></div>
                </dl>
                <button type="button" onClick={() => onConfirm(item)} disabled={loading}><CheckCircle2 aria-hidden="true" size={13} />确认并冻结真实基准</button>
              </details> : null}
            </div>
            {item.status === "RESOLVED" && reflection ? (
              <button className={reflection.status === "CONFIRMED" ? "reflection-confirmed" : ""} type="button" onClick={(event) => onEditReflection(reflection, event.currentTarget)} disabled={loading}>
                <BookCheck aria-hidden="true" size={13} />{reflection.status === "CONFIRMED" ? "已复盘" : "复盘"}
              </button>
            ) : <span className={`observation-status ${statusClass}`} aria-hidden="true" />}
          </article>;
        })}
        {!showAll && observationItems.length > 6 ? <button className="secondary compact observation-show-all" type="button" onClick={() => setShowAll(true)}>查看全部 {observationItems.length} 条</button> : null}
      </div> : <div className="empty-resource">尚无模拟观察；先记录可验证观点，再等待真实交易日结算。</div>}
    </section>
  );
}


export function ObservationDialog({ open, materials, lineageSource = null, onClose, onSubmit, restoreFocusRef }) {
  const dialogRef = useRef(null);
  const closeButtonRef = useRef(null);
  const [form, setForm] = useState(null);
  const [initializedOpen, setInitializedOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [localError, setLocalError] = useState("");
  const requestSessionRef = useRef(0);
  useLayoutEffect(() => {
    requestSessionRef.current += 1;
    if (!open) {
      setInitializedOpen(false);
      setForm(null);
      setBusy(false);
      setLocalError("");
      return;
    }
    setForm({
      symbol: "US.MU",
      direction: "UP",
      horizon_days: 5,
      threshold_pct: 2,
      model_confidence: "",
      methodology_id: "directional_threshold",
      methodology_version: 1,
      thesis: "",
      counter_case: "",
      evidence: { material_ids: [], message_ids: [] },
      ...(lineageSource?.user_decision_id ? {
        user_decision_id: lineageSource.user_decision_id,
        source_portfolio_id: lineageSource.source_portfolio_id,
        source_portfolio_version: lineageSource.source_portfolio_version,
        artifact_id: lineageSource.artifact_id,
        derivation_note: "",
      } : {}),
    });
    setBusy(false);
    setLocalError("");
    setInitializedOpen(true);
  }, [lineageSource, open]);
  const surfaceOpen = Boolean(open && form && initializedOpen);
  useModalFocus({
    open: surfaceOpen,
    containerRef: dialogRef,
    initialFocusRef: closeButtonRef,
    restoreFallbackRef: restoreFocusRef,
    onClose: busy ? null : onClose,
  });
  useEffect(() => {
    if (surfaceOpen && busy) dialogRef.current?.focus({ preventScroll: true });
  }, [busy, surfaceOpen]);
  if (!surfaceOpen) return null;
  const toggleMaterial = (materialId) => {
    const current = form.evidence.material_ids;
    const materialIds = current.includes(materialId)
      ? current.filter((id) => id !== materialId)
      : [...current, materialId];
    setForm({ ...form, evidence: { ...form.evidence, material_ids: materialIds } });
  };
  const submit = async (event) => {
    event.preventDefault();
    if (busy) return;
    if (lineageSource?.user_decision_id && form.derivation_note.trim().length < 3) {
      setLocalError("请填写至少 3 个字的观察推导说明，说明该观察如何验证当前模拟组合。");
      return;
    }
    const requestSession = requestSessionRef.current + 1;
    requestSessionRef.current = requestSession;
    setBusy(true);
    setLocalError("");
    try {
      await onSubmit(form);
    } catch (requestError) {
      if (requestSessionRef.current === requestSession) {
        setLocalError(requestError?.message || "保存观察失败，请检查输入后重试。");
        setBusy(false);
      }
    }
  };
  return (
    <div
      className="dialog-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !busy) onClose();
      }}
    >
      <form
        ref={dialogRef}
        className="dialog observation-dialog"
        role="dialog"
        aria-modal="true"
        aria-label="新建模拟观察"
        aria-busy={busy}
        tabIndex={-1}
        onSubmit={submit}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header><span><FlaskConical aria-hidden="true" size={17} /><strong>新建模拟观察</strong></span><button ref={closeButtonRef} type="button" className="icon-button" aria-label="关闭新建模拟观察" onClick={onClose} disabled={busy}><X aria-hidden="true" size={18} /></button></header>
        <fieldset className="observation-dialog-body" disabled={busy}>
          <legend className="observation-dialog-body-legend">模拟观察设置</legend>
        {lineageSource?.user_decision_id ? (
          <section className="paper-dialog-lineage-source active">
            <GitBranch aria-hidden="true" size={17} />
            <span>
              <small>只读来源 · 前向观察与历史回放互不作为对方输入</small>
              <strong>{lineageSource.source_portfolio_name} · v{lineageSource.source_portfolio_version}</strong>
              <em>
                {lineageSource.selected_option_title} · 产物 v{lineageSource.artifact_version}
                {` · 决定 ${String(lineageSource.user_decision_id).slice(-8)}`}
                {lineageSource.selected_option_id
                  ? ` · AI 首选 ${lineageSource.ai_preferred_option_id || "未记录"} · 用户选择 ${lineageSource.selected_option_id}`
                  : ""}
              </em>
            </span>
          </section>
        ) : null}
        {lineageSource?.user_decision_id ? (
          <label className="paper-derivation-note">观察推导说明
            <textarea
              required
              minLength={3}
              maxLength={1000}
              value={form.derivation_note}
              onChange={(event) => setForm({ ...form, derivation_note: event.target.value })}
              placeholder="说明这条 1 / 5 / 20 日观察验证组合中的哪项假设、条件或失效信号。"
            />
            <small>将与用户决定和模拟组合精确版本一起写入不可变谱系事件。</small>
          </label>
        ) : null}
        <div className="form-grid observation-grid-three">
          <label>标的<select value={form.symbol} onChange={(event) => setForm({ ...form, symbol: event.target.value })}>
            {['US.MU', 'US.SNDK', 'US.WDC', 'US.STX'].map((symbol) => <option key={symbol} value={symbol}>{symbol.replace('US.', '')}</option>)}
          </select></label>
          <label>方向<select value={form.direction} onChange={(event) => setForm({ ...form, direction: event.target.value })}>
            <option value="UP">看涨</option><option value="DOWN">看跌</option><option value="NEUTRAL">中性</option>
          </select></label>
          <label>交易日<select value={form.horizon_days} onChange={(event) => setForm({ ...form, horizon_days: Number(event.target.value) })}>
            <option value={1}>1 日</option><option value={5}>5 日</option><option value={20}>20 日</option>
          </select></label>
        </div>
        <div className="form-grid">
          <label>{form.direction === "NEUTRAL" ? "允许波动阈值（%）" : "最低涨跌阈值（%）"}<input type="number" min={form.direction === "NEUTRAL" ? 0.1 : 0} max="50" step="0.1" required value={form.threshold_pct} onChange={(event) => setForm({ ...form, threshold_pct: event.target.value })} /></label>
          <label>你的主观置信度（可选）<input type="number" min="0" max="100" step="1" value={form.model_confidence} onChange={(event) => setForm({ ...form, model_confidence: event.target.value })} placeholder="不进入 AI 校准" /></label>
        </div>
        <div className="form-grid">
          <label>观察方法 ID<input required value={form.methodology_id} onChange={(event) => setForm({ ...form, methodology_id: event.target.value })} placeholder="例如 directional_threshold" /></label>
          <label>方法版本<input type="number" min="1" max="9999" step="1" required value={form.methodology_version} onChange={(event) => setForm({ ...form, methodology_version: Number(event.target.value) })} /></label>
        </div>
        <label>可验证依据<textarea required value={form.thesis} onChange={(event) => setForm({ ...form, thesis: event.target.value })} placeholder="写清楚为什么、方向、期限和什么结果算命中。" /></label>
        <label>主要反证或失效条件<textarea required value={form.counter_case} onChange={(event) => setForm({ ...form, counter_case: event.target.value })} placeholder="哪些事实出现时，这个判断应被推翻？" /></label>
        {materials?.length ? <fieldset className="observation-evidence">
          <legend>关联共享资料（可多选）</legend>
          {materials.slice(0, 12).map((material) => <label key={material.id}>
            <input type="checkbox" checked={form.evidence.material_ids.includes(material.id)} onChange={() => toggleMaterial(material.id)} />
            <span>{material.title}<small>v{material.version}</small></span>
          </label>)}
          {materials.length > 12 ? (
            <small className="observation-evidence-limit">
              当前选择窗口仅展开前 12 条共享资料，另有 {materials.length - 12} 条未展开。
            </small>
          ) : null}
        </fieldset> : null}
        <div className="material-safety-note"><ShieldCheck aria-hidden="true" size={16} /><span>保存后仍只是提案。再次确认时才会请求富途真实基准价；富途离线不会补造价格。重复或时间窗口重叠的记录不会重复计入胜率。</span></div>
        {localError ? <div className="material-local-error" role="alert">{localError}</div> : null}
        </fieldset>
        <footer><button type="button" className="secondary" onClick={onClose} disabled={busy}>取消</button><button type="submit" className="primary" disabled={busy}>{busy ? <LoaderCircle aria-hidden="true" className="spin" size={14} /> : null}保存待确认提案</button></footer>
      </form>
    </div>
  );
}
