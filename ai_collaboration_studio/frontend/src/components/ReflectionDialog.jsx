import { BookCheck, CheckCircle2, LoaderCircle, ShieldCheck, X } from "lucide-react";
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useModalFocus } from "../useModalFocus";
import {
  reflectionDialogSourceState,
  reflectionFormChanged,
  reflectionFormSubmission,
  reflectionRequestErrorMessage,
  reflectionSaveControl,
} from "../reflectionDialogUi";
import "../styles/reflection-dialog.css";


export function ReflectionDialog({ reflection, open, onClose, onSave, onConfirm, restoreFocusRef }) {
  const dialogRef = useRef(null);
  const closeButtonRef = useRef(null);
  const [form, setForm] = useState(null);
  const [initializedReflection, setInitializedReflection] = useState(null);
  const [busy, setBusy] = useState(false);
  const [localError, setLocalError] = useState("");
  const requestSessionRef = useRef(0);
  const submissionInFlightRef = useRef(false);
  const sourceState = useMemo(
    () => reflection ? reflectionDialogSourceState(reflection) : null,
    [reflection],
  );

  useLayoutEffect(() => {
    requestSessionRef.current += 1;
    submissionInFlightRef.current = false;
    if (!open || !reflection || !sourceState) {
      setInitializedReflection(null);
      setForm(null);
      setBusy(false);
      setLocalError("");
      return;
    }
    setForm(sourceState.form);
    setBusy(false);
    setLocalError("");
    setInitializedReflection(reflection);
  }, [open, reflection, sourceState]);

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
  const source = sourceState.source;
  const formChanged = reflectionFormChanged(form, sourceState.form);
  const draftControl = reflectionSaveControl({
    mode: "draft",
    form,
    sourceState,
    changed: formChanged,
    busy,
    saveHandlerAvailable: typeof onSave === "function",
    confirmHandlerAvailable: typeof onConfirm === "function",
    closeHandlerAvailable: typeof onClose === "function",
  });
  const confirmControl = reflectionSaveControl({
    mode: "confirm",
    form,
    sourceState,
    changed: formChanged,
    busy,
    saveHandlerAvailable: typeof onSave === "function",
    confirmHandlerAvailable: typeof onConfirm === "function",
    closeHandlerAvailable: typeof onClose === "function",
  });

  const updateFormField = (field, value) => {
    setForm((current) => ({ ...current, [field]: value }));
    if (localError) setLocalError("");
  };

  const submit = async (event, shouldConfirm) => {
    event.preventDefault();
    if (submissionInFlightRef.current) return;
    const control = shouldConfirm ? confirmControl : draftControl;
    if (!control.canSubmit) {
      setLocalError(control.instruction);
      return;
    }
    const requestSession = requestSessionRef.current + 1;
    requestSessionRef.current = requestSession;
    submissionInFlightRef.current = true;
    const saveHandler = onSave;
    const confirmHandler = onConfirm;
    const closeHandler = onClose;
    setBusy(true);
    setLocalError("");
    try {
      let saved;
      try {
        saved = await saveHandler(reflection, reflectionFormSubmission(form));
      } catch (saveError) {
        if (requestSessionRef.current === requestSession) {
          setLocalError(reflectionRequestErrorMessage(saveError, "保存复盘失败，请检查输入后重试。"));
        }
        return;
      }
      if (requestSessionRef.current !== requestSession) return;
      if (shouldConfirm) {
        try {
          await confirmHandler(saved);
        } catch (confirmError) {
          if (requestSessionRef.current === requestSession) {
            const detail = reflectionRequestErrorMessage(confirmError, "");
            setLocalError(detail
              ? `草稿已保存，但确认入记忆失败：${detail}`
              : "草稿已保存，但确认入记忆失败。");
          }
          return;
        }
      }
      if (requestSessionRef.current !== requestSession) return;
      try {
        closeHandler();
      } catch (closeError) {
        const detail = reflectionRequestErrorMessage(closeError, "");
        setLocalError(detail
          ? `${shouldConfirm ? "复盘已确认" : "草稿已保存"}，但窗口关闭失败：${detail}`
          : `${shouldConfirm ? "复盘已确认" : "草稿已保存"}，但窗口关闭失败。`);
      }
    } finally {
      if (requestSessionRef.current === requestSession) {
        submissionInFlightRef.current = false;
        setBusy(false);
      }
    }
  };

  return (
    <div
      className="dialog-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !busy) onClose?.();
      }}
    >
      <form
        ref={dialogRef}
        className="dialog reflection-dialog"
        role="dialog"
        aria-modal="true"
        aria-label="观察结果复盘"
        aria-busy={busy}
        data-draft-state={draftControl.phase}
        data-confirm-state={confirmControl.phase}
        tabIndex={-1}
        onSubmit={(event) => submit(event, false)}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header>
          <span><BookCheck size={17} aria-hidden="true" /><strong>观察结果复盘</strong><small className={`reflection-status ${sourceState.statusClass}`}>{sourceState.confirmed ? "已纳入记忆" : "草稿"}</small></span>
          <button ref={closeButtonRef} type="button" className="icon-button" aria-label="关闭观察结果复盘" onClick={() => onClose?.()} disabled={busy}><X size={18} aria-hidden="true" /></button>
        </header>
        <fieldset className="reflection-dialog-body" disabled={busy}>
          <legend className="reflection-dialog-body-legend">观察结果与复盘内容</legend>
        <div className="reflection-source">
          <span><small>来源观察</small><strong>{source.symbol} · {source.directionText} · {source.horizonText}</strong></span>
          <span><small>真实结果</small><strong>{source.hitText} · {source.returnText}</strong></span>
          <span><small>同行等权收益</small><strong>{source.peerReturnText}</strong></span>
          <span><small>相对同行</small><strong>{source.relativeText}</strong></span>
          <span><small>结果时点</small><strong>{source.outcomeTime}</strong></span>
          <span><small>审计指纹</small><strong title={source.hash || undefined}>{source.hashShort}</strong></span>
        </div>
        <section className="reflection-permit-ledger" aria-label="复盘操作许可">
          <span><small>草稿保存</small><strong>{draftControl.instruction}</strong></span>
          <span><small>确认入记忆</small><strong>{confirmControl.instruction}</strong></span>
        </section>
        {!sourceState.integrityOk ? <div className="reflection-source-warning" role="alert"><strong>来源审计不完整</strong><span>{sourceState.issues[0]} {sourceState.identityOk ? "可继续保存草稿，但不能确认入记忆。" : "记录身份或版本无效，草稿与确认操作均不可用。"}</span></div> : null}
        <label>本次教训<textarea required value={form.lesson} onChange={(event) => updateFormField("lesson", event.target.value)} placeholder="哪些判断得到支持，哪些假设被结果否定？不要只写命中或未命中。" /></label>
        <label>不能外推的部分<textarea required value={form.caveat} onChange={(event) => updateFormField("caveat", event.target.value)} placeholder="说明样本、市场状态、证据或因果上的限制。" /></label>
        <label>下一次验证条件<textarea required value={form.next_test} onChange={(event) => updateFormField("next_test", event.target.value)} placeholder="下次要新增什么证据、改变什么阈值，或在什么市场状态下再次验证？" /></label>
        <div className="material-safety-note"><ShieldCheck size={16} aria-hidden="true" /><span>只有你确认后的反思才会进入以后讨论；系统始终携带原观察 ID、真实结果和审计指纹，单个案例不会自动变成胜率或因果规律。</span></div>
        {localError ? <div className="material-local-error" role="alert">{localError}</div> : null}
        </fieldset>
        <footer className="reflection-dialog-footer">
          <button type="button" className="secondary" onClick={() => onClose?.()} disabled={busy}>取消</button>
          <span>
            <button type="submit" className="secondary" disabled={!draftControl.canSubmit}>{busy ? <LoaderCircle className="spin" size={14} aria-hidden="true" /> : null}保存草稿</button>
            <button type="button" className="primary" disabled={!confirmControl.canSubmit} onClick={(event) => submit(event, true)}><CheckCircle2 size={14} aria-hidden="true" />确认并纳入记忆</button>
          </span>
        </footer>
      </form>
    </div>
  );
}
