import { BookCheck, CheckCircle2, LoaderCircle, ShieldCheck, X } from "lucide-react";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { useModalFocus } from "../useModalFocus";
import "../styles/reflection-dialog.css";


const directionText = { UP: "看涨", DOWN: "看跌", NEUTRAL: "中性" };


export function ReflectionDialog({ reflection, open, onClose, onSave, onConfirm, restoreFocusRef }) {
  const dialogRef = useRef(null);
  const closeButtonRef = useRef(null);
  const [form, setForm] = useState(null);
  const [initializedReflection, setInitializedReflection] = useState(null);
  const [busy, setBusy] = useState(false);
  const [localError, setLocalError] = useState("");

  useLayoutEffect(() => {
    if (!open || !reflection) {
      setInitializedReflection(null);
      setForm(null);
      setBusy(false);
      setLocalError("");
      return;
    }
    setForm({
      lesson: reflection.lesson || "",
      caveat: reflection.caveat || "",
      next_test: reflection.next_test || "",
    });
    setBusy(false);
    setLocalError("");
    setInitializedReflection(reflection);
  }, [open, reflection]);

  const surfaceOpen = Boolean(open && reflection && form && initializedReflection === reflection);
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
  const source = reflection.source_snapshot || {};

  const submit = async (event, shouldConfirm) => {
    event.preventDefault();
    setBusy(true);
    setLocalError("");
    try {
      const saved = await onSave(reflection, form);
      if (shouldConfirm) await onConfirm(saved);
      onClose();
    } catch (requestError) {
      setLocalError(requestError.message);
      setBusy(false);
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
        className="dialog reflection-dialog"
        role="dialog"
        aria-modal="true"
        aria-label="观察结果复盘"
        aria-busy={busy}
        tabIndex={-1}
        onSubmit={(event) => submit(event, false)}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header>
          <span><BookCheck size={17} /><strong>观察结果复盘</strong><small className={`reflection-status ${reflection.status.toLowerCase()}`}>{reflection.status === "CONFIRMED" ? "已纳入记忆" : "草稿"}</small></span>
          <button ref={closeButtonRef} type="button" className="icon-button" aria-label="关闭观察结果复盘" onClick={onClose} disabled={busy}><X size={18} /></button>
        </header>
        <div className="reflection-source">
          <span><small>来源观察</small><strong>{source.symbol?.replace("US.", "")} · {directionText[source.direction] || source.direction} · {source.horizon_days} 日</strong></span>
          <span><small>真实结果</small><strong>{source.hit ? "命中" : "未命中"} · {Number(source.return_pct).toFixed(2)}%</strong></span>
          <span><small>同行等权收益</small><strong>{source.benchmark_result?.peer_equal_weight_return_pct !== undefined ? `${Number(source.benchmark_result.peer_equal_weight_return_pct).toFixed(2)}%` : "不可用"}</strong></span>
          <span><small>相对同行</small><strong>{source.relative_return_pct !== undefined ? `${Number(source.relative_return_pct) > 0 ? "+" : ""}${Number(source.relative_return_pct).toFixed(2)}% · ${source.relative_hit ? "相对命中" : "相对未命中"}` : "不可用"}</strong></span>
          <span><small>结果时点</small><strong>{source.outcome_time || "未知"}</strong></span>
          <span><small>审计指纹</small><strong>{reflection.source_snapshot_hash?.slice(0, 12)}</strong></span>
        </div>
        <label>本次教训<textarea required value={form.lesson} onChange={(event) => setForm({ ...form, lesson: event.target.value })} placeholder="哪些判断得到支持，哪些假设被结果否定？不要只写命中或未命中。" /></label>
        <label>不能外推的部分<textarea required value={form.caveat} onChange={(event) => setForm({ ...form, caveat: event.target.value })} placeholder="说明样本、市场状态、证据或因果上的限制。" /></label>
        <label>下一次验证条件<textarea required value={form.next_test} onChange={(event) => setForm({ ...form, next_test: event.target.value })} placeholder="下次要新增什么证据、改变什么阈值，或在什么市场状态下再次验证？" /></label>
        <div className="material-safety-note"><ShieldCheck size={16} /><span>只有你确认后的反思才会进入以后讨论；系统始终携带原观察 ID、真实结果和审计指纹，单个案例不会自动变成胜率或因果规律。</span></div>
        {localError ? <div className="material-local-error">{localError}</div> : null}
        <footer className="reflection-dialog-footer">
          <button type="button" className="secondary" onClick={onClose} disabled={busy}>取消</button>
          <span>
            <button type="submit" className="secondary" disabled={busy}>{busy ? <LoaderCircle className="spin" size={14} /> : null}保存草稿</button>
            <button type="button" className="primary" disabled={busy} onClick={(event) => submit(event, true)}><CheckCircle2 size={14} />确认并纳入记忆</button>
          </span>
        </footer>
      </form>
    </div>
  );
}
