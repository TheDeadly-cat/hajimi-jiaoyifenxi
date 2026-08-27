import {
  AlertTriangle,
  CheckCircle2,
  ClipboardCheck,
  Copy,
  ExternalLink,
  LoaderCircle,
  RefreshCw,
  ShieldCheck,
  X,
} from "lucide-react";
import { useEffect, useId, useMemo, useRef, useState } from "react";
import { api } from "../api";
import {
  CHATGPT_CONTINUATION_URL,
  MANUAL_CHATGPT_MODES,
  formatManualChatGPTContextSize,
  formatManualChatGPTCostEstimate,
  manualChatGPTIndependenceLabel,
  manualChatGPTPrimaryAction,
  manualChatGPTReviewClientRequestId,
  manualChatGPTStateView,
  normalizeManualChatGPT,
  shortManualChatGPTHash,
} from "../manualChatGPT";
import { useModalFocus } from "../useModalFocus";
import "../styles/manual-chatgpt.css";

function errorMessage(error, fallback = "ChatGPT 协作步骤失败。") {
  return error instanceof Error && error.message ? error.message.slice(0, 1000) : fallback;
}

async function writeClipboard(value) {
  if (!globalThis.navigator?.clipboard?.writeText) {
    throw new Error("浏览器未开放剪贴板写入，请展开下方任务提示并手动复制。 ");
  }
  await globalThis.navigator.clipboard.writeText(value);
}

async function readClipboard() {
  if (!globalThis.navigator?.clipboard?.readText) {
    throw new Error("浏览器未开放剪贴板读取，请先粘贴到文本框。 ");
  }
  return globalThis.navigator.clipboard.readText();
}

export function ChatGPTCollaborationDialog({
  open,
  roomId,
  initialObjective = "",
  restoreFocusRef,
  onClose,
}) {
  const titleId = useId();
  const descriptionId = useId();
  const dialogRef = useRef(null);
  const closeButtonRef = useRef(null);
  const operationRef = useRef(0);
  const [objective, setObjective] = useState("");
  const [mode, setMode] = useState("standard");
  const [session, setSession] = useState(null);
  const [sessions, setSessions] = useState([]);
  const [importText, setImportText] = useState("");
  const [reviewProvider, setReviewProvider] = useState("");
  const [reviewModel, setReviewModel] = useState("");
  const [selectedOptionId, setSelectedOptionId] = useState("");
  const [freezeAcknowledged, setFreezeAcknowledged] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const view = useMemo(
    () => (session ? manualChatGPTStateView(session) : null),
    [session],
  );
  const adoptSession = (nextSession) => {
    setSession(nextSession || null);
    if (!nextSession?.id) return;
    setSessions((current) => [
      nextSession,
      ...current.filter((item) => item?.id !== nextSession.id),
    ].slice(0, 30));
  };

  useEffect(() => {
    if (!open || !roomId) return undefined;
    const sequence = operationRef.current + 1;
    operationRef.current = sequence;
    setObjective(initialObjective.trim());
    setImportText("");
    setError("");
    setBusy(true);
    api.listManualChatGPT(roomId)
      .then((data) => {
        if (operationRef.current !== sequence) return;
        const history = Array.isArray(data.manual_chatgpt_sessions)
          ? data.manual_chatgpt_sessions
          : [];
        const latest = history[0] || null;
        setSessions(history);
        setSession(latest);
        if (latest?.mode) setMode(latest.mode);
        const normalized = latest ? normalizeManualChatGPT(latest) : null;
        setReviewProvider(normalized?.apiReviewProvider || "");
        setReviewModel(normalized?.apiReviewModel || "");
        setSelectedOptionId(normalized?.confirmedOptionId || "");
        setFreezeAcknowledged(false);
      })
      .catch((requestError) => {
        if (operationRef.current !== sequence) return;
        setSession(null);
        setSessions([]);
        setError(errorMessage(requestError, "无法读取最近的 ChatGPT 协作任务。"));
      })
      .finally(() => {
        if (operationRef.current === sequence) setBusy(false);
      });
    return () => { operationRef.current += 1; };
  }, [initialObjective, open, roomId]);

  useModalFocus({
    open,
    containerRef: dialogRef,
    initialFocusRef: closeButtonRef,
    restoreFallbackRef: restoreFocusRef,
    onClose: busy ? null : onClose,
  });

  if (!open) return null;

  const createBundle = async () => {
    const cleanObjective = objective.trim();
    if (!cleanObjective) {
      setError("请先输入研究问题。 ");
      return;
    }
    const sequence = operationRef.current + 1;
    operationRef.current = sequence;
    setBusy(true);
    setError("");
    try {
      const data = await api.createManualChatGPT(roomId, {
        objective: cleanObjective,
        mode,
      });
      if (operationRef.current !== sequence) return;
      adoptSession(data.manual_chatgpt);
      setImportText("");
      setSelectedOptionId("");
      setFreezeAcknowledged(false);
    } catch (requestError) {
      if (operationRef.current === sequence) setError(errorMessage(requestError));
    } finally {
      if (operationRef.current === sequence) setBusy(false);
    }
  };

  const copyAndOpen = async () => {
    const current = normalizeManualChatGPT(session);
    if (!current.taskPrompt) {
      setError("冻结任务包不可用。 ");
      return;
    }
    const sequence = operationRef.current + 1;
    operationRef.current = sequence;
    setBusy(true);
    setError("");
    try {
      await writeClipboard(current.taskPrompt);
      const data = await api.dispatchManualChatGPT(roomId, current.id);
      if (operationRef.current !== sequence) return;
      adoptSession(data.manual_chatgpt);
    } catch (requestError) {
      if (operationRef.current === sequence) setError(errorMessage(requestError));
    } finally {
      if (operationRef.current === sequence) setBusy(false);
    }
  };

  const confirmManualCopy = async () => {
    const current = normalizeManualChatGPT(session);
    if (!current.taskPrompt || current.state !== "BUNDLE_READY") {
      setError("当前任务提示不可进入人工导入步骤。 ");
      return;
    }
    const sequence = operationRef.current + 1;
    operationRef.current = sequence;
    setBusy(true);
    setError("");
    try {
      const data = await api.dispatchManualChatGPT(roomId, current.id);
      if (operationRef.current !== sequence) return;
      adoptSession(data.manual_chatgpt);
    } catch (requestError) {
      if (operationRef.current === sequence) setError(errorMessage(requestError));
    } finally {
      if (operationRef.current === sequence) setBusy(false);
    }
  };

  const importClipboard = async () => {
    const current = normalizeManualChatGPT(session);
    const sequence = operationRef.current + 1;
    operationRef.current = sequence;
    setBusy(true);
    setError("");
    try {
      const content = importText.trim() || (await readClipboard()).trim();
      if (!content) throw new Error("剪贴板和导入框都是空的。 ");
      setImportText(content);
      const data = await api.importManualChatGPT(roomId, current.id, content);
      if (operationRef.current !== sequence) return;
      adoptSession(data.manual_chatgpt);
      if (data.accepted) setImportText("");
    } catch (requestError) {
      if (operationRef.current === sequence) setError(errorMessage(requestError));
    } finally {
      if (operationRef.current === sequence) setBusy(false);
    }
  };

  const copyRepairPrompt = async () => {
    if (!view?.repairPrompt) {
      setError("当前没有可复制的修复提示。 ");
      return;
    }
    setBusy(true);
    setError("");
    try {
      await writeClipboard(view.repairPrompt);
    } catch (clipboardError) {
      setError(errorMessage(clipboardError, "复制修复提示失败。"));
    } finally {
      setBusy(false);
    }
  };

  const runIndependentReview = async () => {
    const current = normalizeManualChatGPT(session);
    const provider = reviewProvider.trim().toLowerCase();
    if (!provider) {
      setError("请显式填写独立审查通道。 ");
      return;
    }
    const clientRequestId = manualChatGPTReviewClientRequestId(current);
    if (!clientRequestId) {
      setError("当前审查绑定不完整，请重新读取任务记录。 ");
      return;
    }
    const sequence = operationRef.current + 1;
    operationRef.current = sequence;
    setBusy(true);
    setError("");
    try {
      const data = await api.runManualChatGPTReview(roomId, current.id, {
        provider,
        model: reviewModel.trim(),
        client_request_id: clientRequestId,
        expected_result_sha256: current.resultSha256,
      });
      if (operationRef.current !== sequence) return;
      adoptSession(data.manual_chatgpt);
      const reviewed = normalizeManualChatGPT(data.manual_chatgpt);
      setReviewProvider(reviewed.apiReviewProvider || provider);
      setReviewModel(reviewed.apiReviewModel || reviewModel.trim());
      setSelectedOptionId("");
      setFreezeAcknowledged(false);
    } catch (requestError) {
      if (operationRef.current === sequence) setError(errorMessage(requestError));
    } finally {
      if (operationRef.current === sequence) setBusy(false);
    }
  };

  const recoverIndependentReview = async () => {
    const current = normalizeManualChatGPT(session);
    if (!current.reviewRecoveryEligible || !current.reviewRecoveryAcknowledgement) {
      setError("当前审查不满足零调用孤儿恢复条件。 ");
      return;
    }
    const sequence = operationRef.current + 1;
    operationRef.current = sequence;
    setBusy(true);
    setError("");
    try {
      const data = await api.recoverManualChatGPTReview(roomId, current.id, {
        expected_result_sha256: current.resultSha256,
        acknowledgement: current.reviewRecoveryAcknowledgement,
      });
      if (operationRef.current !== sequence) return;
      adoptSession(data.manual_chatgpt);
    } catch (requestError) {
      if (operationRef.current === sequence) setError(errorMessage(requestError));
    } finally {
      if (operationRef.current === sequence) setBusy(false);
    }
  };

  const confirmAndFreeze = async () => {
    const current = normalizeManualChatGPT(session);
    if (!selectedOptionId || !freezeAcknowledged) {
      setError("请选择决定并确认研究只读边界。 ");
      return;
    }
    const sequence = operationRef.current + 1;
    operationRef.current = sequence;
    setBusy(true);
    setError("");
    try {
      const data = await api.freezeManualChatGPT(roomId, current.id, {
        expected_result_sha256: current.resultSha256,
        decision_card_sha256: current.decisionCardSha256,
        selected_option_id: selectedOptionId,
        acknowledgement: "RESEARCH_ONLY_USER_DECISION",
      });
      if (operationRef.current !== sequence) return;
      adoptSession(data.manual_chatgpt);
      setFreezeAcknowledged(false);
    } catch (requestError) {
      if (operationRef.current === sequence) setError(errorMessage(requestError));
    } finally {
      if (operationRef.current === sequence) setBusy(false);
    }
  };

  const resetForNewBundle = () => {
    setSession(null);
    setImportText("");
    setError("");
    setSelectedOptionId("");
    setFreezeAcknowledged(false);
    setObjective(initialObjective.trim() || view?.objective || "");
    if (view?.mode) setMode(view.mode);
  };

  const selectHistoricalSession = (nextSession) => {
    setSession(nextSession);
    const normalized = normalizeManualChatGPT(nextSession);
    if (normalized.mode) setMode(normalized.mode);
    setReviewProvider(normalized.apiReviewProvider || "");
    setReviewModel(normalized.apiReviewModel || "");
    setSelectedOptionId(normalized.confirmedOptionId || "");
    setFreezeAcknowledged(false);
    setImportText("");
    setError("");
  };

  const primaryAction = view
    ? manualChatGPTPrimaryAction(view, {
      hasImportText: Boolean(importText.trim()),
      hasReviewRoute: Boolean(reviewProvider.trim()),
      hasDecision: Boolean(selectedOptionId),
      freezeAcknowledged,
    })
    : {
      id: "create_bundle",
      label: "冻结任务包",
      enabled: Boolean(objective.trim()),
    };

  const executePrimaryAction = () => {
    if (primaryAction.id === "create_bundle") return createBundle();
    if (primaryAction.id === "copy_and_open_chatgpt") return copyAndOpen();
    if (["import_clipboard", "reimport_fixed_json"].includes(primaryAction.id)) return importClipboard();
    if (primaryAction.id === "copy_repair_prompt") return copyRepairPrompt();
    if (primaryAction.id === "reset_for_new_bundle") return resetForNewBundle();
    if (primaryAction.id === "recover_api_review") return recoverIndependentReview();
    if (primaryAction.id === "run_api_review") return runIndependentReview();
    if (primaryAction.id === "confirm_freeze") return confirmAndFreeze();
    return undefined;
  };

  const renderPrimaryAction = () => {
    const disabled = busy || !primaryAction.enabled;
    const actionIcon = busy || primaryAction.id === "pending"
      ? <LoaderCircle className="spin" size={16} />
      : primaryAction.id === "copy_and_open_chatgpt"
        ? <ExternalLink size={16} />
        : ["import_clipboard", "reimport_fixed_json"].includes(primaryAction.id)
          ? <ClipboardCheck size={16} />
          : primaryAction.id === "copy_repair_prompt"
            ? <Copy size={16} />
            : primaryAction.id === "reset_for_new_bundle"
              ? <RefreshCw size={16} />
              : <ShieldCheck size={16} />;
    if (primaryAction.id === "copy_and_open_chatgpt") {
      return (
        <a
          className="primary"
          href={CHATGPT_CONTINUATION_URL}
          target="_blank"
          rel="noopener noreferrer"
          aria-disabled={disabled ? "true" : undefined}
          tabIndex={disabled ? -1 : undefined}
          onClick={(event) => {
            if (disabled) {
              event.preventDefault();
              return;
            }
            executePrimaryAction();
          }}
        >
          {actionIcon}
          {primaryAction.label}
        </a>
      );
    }
    return (
      <button
        className="primary"
        type="button"
        disabled={disabled}
        onClick={primaryAction.id === "pending" ? undefined : executePrimaryAction}
      >
        {actionIcon}
        {primaryAction.label}
      </button>
    );
  };

  const importVisible = view && ["WAITING_FOR_CHATGPT", "IMPORT_REJECTED"].includes(view.state);
  const finalDecisionState = Boolean(
    view && ["READY_FOR_DECISION", "FROZEN"].includes(view.state),
  );
  const apiReviewRecordsSection = view?.apiReviewRecords.length ? (
    <section className="manual-chatgpt-api-reviews" aria-label="独立 API 审查记录">
      <header>
        <div><span>INDEPENDENT API REVIEWS</span><h3>独立调用审查</h3></div>
        <small>{view.apiReviewCompletedCalls}/{view.apiReviewExpectedCalls} · {view.apiReviewDistinctCalls ? "调用记录各自独立" : "独立性未通过"}</small>
      </header>
      <div className="manual-chatgpt-review-grid">
        {view.apiReviewRecords.map((review) => (
          <article key={`${review.reviewIndex}-${review.reviewKind}`}>
            <div>
              <strong>{review.reviewKind}</strong>
              <span className={`manual-chatgpt-verdict ${review.verdict}`}>{review.verdict}</span>
            </div>
            <p>{review.summary}</p>
            {review.findings.length ? (
              <ul className="manual-chatgpt-review-findings">
                {review.findings.map((finding, index) => (
                  <li key={`${review.reviewIndex}-${index}`}>
                    <strong>{finding.severity || "finding"} · {finding.claim}</strong>
                    <span>{finding.rationale}</span>
                  </li>
                ))}
              </ul>
            ) : null}
            <small>{review.provider} · {review.responseModel || review.requestedModel} · {manualChatGPTIndependenceLabel(review.independenceClassification)}</small>
          </article>
        ))}
      </div>
    </section>
  ) : null;
  const importedPanelsSection = view?.result?.panels?.length ? (
    <section className="manual-chatgpt-results" aria-label="已导入的 ChatGPT Panel">
      <header>
        <div><span>IMPORTED PANELS</span><h3>已导入的角色视角</h3></div>
        <small>独立性为声明元数据，未由手工导入证明</small>
      </header>
      {view.result.panels.map((panel) => (
        <article key={panel.panel_id}>
          <div className="manual-chatgpt-result-head">
            <strong>第 {panel.call_index}/{view.chatGPTPanels} 回合 · {panel.panel_kind}</strong>
            <span>{manualChatGPTIndependenceLabel(panel.declared_independence)} · 未核验</span>
          </div>
          <p>{panel.summary}</p>
          <p className="manual-chatgpt-conclusion"><strong>结论</strong>{panel.conclusion}</p>
          <details>
            <summary>{panel.role_views?.length || 0} 个角色视角</summary>
            <ul>{(panel.role_views || []).map((role) => (
              <li key={role.role_id}>
                <strong>{role.role_id}</strong>
                <span>{role.assessment}</span>
                <small>不确定性：{role.uncertainty}</small>
                {Array.isArray(role.evidence_refs) && role.evidence_refs.length
                  ? <small>证据引用：{role.evidence_refs.join("、")}</small>
                  : null}
              </li>
            ))}</ul>
          </details>
        </article>
      ))}
    </section>
  ) : null;
  return (
    <div className="dialog-backdrop manual-chatgpt-backdrop" role="presentation">
      <section
        ref={dialogRef}
        className="manual-chatgpt-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={descriptionId}
        aria-busy={busy ? "true" : "false"}
        tabIndex={-1}
      >
        <header>
          <div>
            <span className="manual-chatgpt-kicker">CHATGPT COLLABORATION SEAT</span>
            <h2 id={titleId}>ChatGPT 协作席位</h2>
            <p id={descriptionId}>人工复制与导入，不会自动调用 ChatGPT Provider；角色视角不冒充独立模型意见。</p>
          </div>
          <button
            ref={closeButtonRef}
            className="icon-button"
            type="button"
            aria-label="关闭 ChatGPT 协作席位"
            disabled={busy}
            onClick={onClose}
          ><X size={18} /></button>
        </header>

        <div className="manual-chatgpt-body">
          {sessions.length ? (
            <nav className="manual-chatgpt-history" aria-label="ChatGPT 协作任务列表">
              <div>
                <strong>任务记录</strong>
                <span>{sessions.length} 个可恢复任务</span>
              </div>
              <div className="manual-chatgpt-history-list">
                {sessions.map((item) => {
                  const itemView = manualChatGPTStateView(item);
                  return (
                    <button
                      key={itemView.id}
                      type="button"
                      className={itemView.id === view?.id ? "active" : ""}
                      aria-current={itemView.id === view?.id ? "true" : undefined}
                      disabled={busy}
                      onClick={() => selectHistoricalSession(item)}
                    >
                      <strong>{itemView.objective || "未命名协作任务"}</strong>
                      <span>{itemView.label}</span>
                    </button>
                  );
                })}
              </div>
            </nav>
          ) : null}
          {!view ? (
            <section className="manual-chatgpt-setup" aria-label="创建 ChatGPT 协作任务">
              <label>
                研究问题
                <textarea
                  value={objective}
                  onChange={(event) => setObjective(event.target.value)}
                  maxLength={4000}
                  disabled={busy}
                  placeholder="输入这次需要研究和决策的问题…"
                />
              </label>
              <fieldset disabled={busy}>
                <legend>协作深度</legend>
                <div className="manual-chatgpt-modes">
                  {MANUAL_CHATGPT_MODES.map((preset) => (
                    <label key={preset.id} className={mode === preset.id ? "selected" : ""}>
                      <input
                        type="radio"
                        name="manual-chatgpt-mode"
                        value={preset.id}
                        checked={mode === preset.id}
                        onChange={() => setMode(preset.id)}
                      />
                      <strong>{preset.label}</strong>
                      <small>{preset.panels} 次 ChatGPT 回合 · {preset.reviews} 次独立 API</small>
                    </label>
                  ))}
                </div>
              </fieldset>
            </section>
          ) : (
            <>
              <section className={`manual-chatgpt-state ${view.tone}`} role="status" aria-live="polite">
                {view.integrityOk ? <CheckCircle2 size={20} /> : <AlertTriangle size={20} />}
                <div><strong>{view.label}</strong><span>{view.detail}</span></div>
              </section>
              {view.reviewRecoveryEligible ? (
                <section className="manual-chatgpt-recovery-note" role="status">
                  <AlertTriangle size={18} />
                  <div>
                    <strong>发现零调用的孤儿审查</strong>
                    <span>只有在没有任何 Provider 预留、调用或审查结果时，才允许显式重新授权；历史封印会保留。</span>
                  </div>
                </section>
              ) : null}
              <section className="manual-chatgpt-summary" aria-label="冻结任务包摘要">
                <div><span>模式</span><strong>{MANUAL_CHATGPT_MODES.find((item) => item.id === view.mode)?.label || view.mode}</strong></div>
                <div><span>ChatGPT 回合 / Panel</span><strong>{view.chatGPTPanels}</strong></div>
                <div><span>独立 API 审查</span><strong>{view.apiReviews}</strong></div>
                <div><span>角色视角</span><strong>{view.roleCount}</strong></div>
                <div><span>证据索引</span><strong>{view.evidenceCount}</strong></div>
                <div><span>上下文大小</span><strong>{formatManualChatGPTContextSize(view)}</strong></div>
                <div>
                  <span>预计 API 成本</span>
                  <strong title={view.costRateCardLabel || view.costReasonCode || "未配置 API 审查费率"}>
                    {formatManualChatGPTCostEstimate(view)}
                  </strong>
                </div>
                <div><span>上下文哈希</span><strong title={view.contextSha256}>{shortManualChatGPTHash(view.contextSha256)}</strong></div>
              </section>
              <p className="manual-chatgpt-estimate-note">
                Token 为确定性近似；成本仅估算独立 API 审查，不含人工 ChatGPT 订阅。未配置费率时保持“待配置”，不会显示为零成本。
                ChatGPT 回合数是人工操作协议，导入本身不能证明真实模型来源或调用独立性。
              </p>
              <section className="manual-chatgpt-objective">
                <span>冻结问题</span>
                <p>{view.objective}</p>
              </section>
              {view.taskPrompt && ["BUNDLE_READY", "WAITING_FOR_CHATGPT"].includes(view.state) ? (
                <details className="manual-chatgpt-prompt-fallback">
                  <summary>剪贴板受限？展开并手动复制任务提示</summary>
                  <label>
                    只读任务提示
                    <textarea
                      value={view.taskPrompt}
                      readOnly
                      spellCheck="false"
                      onFocus={(event) => event.currentTarget.select()}
                    />
                  </label>
                  <p>全选并复制完整提示。确认只会进入人工导入步骤，不会调用 Provider。</p>
                  {view.state === "BUNDLE_READY" ? (
                    <button className="secondary" type="button" disabled={busy} onClick={confirmManualCopy}>
                      <ClipboardCheck size={16} />我已手动复制，进入导入
                    </button>
                  ) : null}
                </details>
              ) : null}
              {importVisible ? (
                <label className="manual-chatgpt-import">
                  ChatGPT 返回的唯一 JSON 对象
                  <textarea
                    value={importText}
                    onChange={(event) => setImportText(event.target.value)}
                    disabled={busy}
                    placeholder="可直接粘贴；留空时点击按钮会读取剪贴板。"
                  />
                </label>
              ) : null}
              {view.state === "IMPORT_REJECTED" && view.issues.length ? (
                <section className="manual-chatgpt-issues" aria-label="导入字段错误">
                  <h3>精确字段错误</h3>
                  <ol>{view.issues.map((issue) => (
                    <li key={`${issue.path}\u0000${issue.code}`}>
                      <code>{issue.path}</code><strong>{issue.code}</strong><span>{issue.message}</span>
                    </li>
                  ))}</ol>
                  {view.repairPrompt && importText.trim() ? (
                    <button
                      className="secondary"
                      type="button"
                      disabled={busy}
                      onClick={copyRepairPrompt}
                    >
                      <Copy size={16} />
                      复制修复提示
                    </button>
                  ) : null}
                </section>
              ) : null}
              {["NEEDS_USER_ACTION", "BUDGET_BLOCKED"].includes(view.state) && view.issues.length ? (
                <section className="manual-chatgpt-issues" aria-label="独立审查诊断">
                  <h3>独立审查未通过</h3>
                  <ol>{view.issues.map((issue) => (
                    <li key={`${issue.path}\u0000${issue.code}`}>
                      <code>{issue.path}</code><strong>{issue.code}</strong><span>{issue.message}</span>
                    </li>
                  ))}</ol>
                </section>
              ) : null}
              {view.state === "API_REVIEW" ? (
                <section className="manual-chatgpt-boundary">
                  <ShieldCheck size={18} />
                  <div>
                    <strong>独立审查授权</strong>
                    <span>系统将按冻结预算发起 {view.apiReviews} 个真实、分开的调用；角色视角不会计入独立意见。</span>
                    {view.declaredModel ? <small>声明模型：{view.declaredModel}（用户声明，非可信审计事实）</small> : null}
                    {view.apiReviewMigrationRequired ? (
                      <small className="manual-chatgpt-blocked-note">当前数据库尚未迁移审查表；正式迁移需单独授权。</small>
                    ) : null}
                    <div className="manual-chatgpt-review-route">
                      <label>
                        独立审查通道（技术 ID）
                        <input
                          value={reviewProvider}
                          onChange={(event) => setReviewProvider(event.target.value)}
                          maxLength={80}
                          disabled={busy}
                          placeholder="例如 openai"
                          autoComplete="off"
                          spellCheck="false"
                        />
                      </label>
                      <label>
                        模型（留空使用审查通道已配置的默认值）
                        <input
                          value={reviewModel}
                          onChange={(event) => setReviewModel(event.target.value)}
                          maxLength={160}
                          disabled={busy}
                          autoComplete="off"
                          spellCheck="false"
                        />
                      </label>
                    </div>
                  </div>
                </section>
              ) : null}
              {finalDecisionState ? null : apiReviewRecordsSection}
              {view.decisionCardSha256 ? (
                <section className="manual-chatgpt-decision-card" aria-label="确定性决定卡">
                  <header>
                    <div><span>DETERMINISTIC DECISION CARD</span><h3>决定卡</h3></div>
                    <small title={view.decisionCardSha256}>{shortManualChatGPTHash(view.decisionCardSha256)}</small>
                  </header>
                  <p>{view.decisionCard.summary}</p>
                  {view.decisionCard.blockingFindings.length ? (
                    <div className="manual-chatgpt-decision-blockers">
                      <strong>存在阻断项，不能冻结</strong>
                      <ul>{view.decisionCard.blockingFindings.map((finding, index) => (
                        <li key={`${finding.reviewKind}-${index}`}>
                          <strong>{finding.claim}</strong>
                          {finding.rationale ? <span>{finding.rationale}</span> : null}
                        </li>
                      ))}</ul>
                    </div>
                  ) : null}
                  <fieldset disabled={busy || view.state !== "READY_FOR_DECISION"}>
                    <legend>选择你的最终决定</legend>
                    <div className="manual-chatgpt-decision-options">
                      {view.decisionCard.options.map((option) => (
                        <label key={option.optionId} className={selectedOptionId === option.optionId ? "selected" : ""}>
                          <input
                            type="radio"
                            name="manual-chatgpt-decision"
                            value={option.optionId}
                            checked={(selectedOptionId || view.confirmedOptionId) === option.optionId}
                            onChange={() => setSelectedOptionId(option.optionId)}
                          />
                          <span>
                            <strong>{option.title}</strong>
                            <small>{option.rationale}</small>
                            {option.risks.length ? (
                              <ul className="manual-chatgpt-option-risks">
                                {option.risks.map((risk, index) => <li key={`${option.optionId}-risk-${index}`}>{risk}</li>)}
                              </ul>
                            ) : null}
                            {option.optionId === view.decisionCard.importedRecommendedOptionId ? <em>ChatGPT 导入建议 · 非系统核验推荐</em> : null}
                          </span>
                        </label>
                      ))}
                    </div>
                  </fieldset>
                  {view.decisionCard.nonblockingFindings.length || view.decisionCard.openQuestions.length ? (
                    <section className="manual-chatgpt-decision-notes" aria-label="非阻断发现与开放问题">
                      {view.decisionCard.nonblockingFindings.length ? (
                        <div>
                          <strong>仍需留意</strong>
                          <ul>{view.decisionCard.nonblockingFindings.map((finding, index) => (
                            <li key={`${finding.reviewKind}-note-${index}`}>{finding.severity} · {finding.claim}</li>
                          ))}</ul>
                        </div>
                      ) : null}
                      {view.decisionCard.openQuestions.length ? (
                        <div>
                          <strong>开放问题</strong>
                          <ul>{view.decisionCard.openQuestions.map((question, index) => (
                            <li key={`open-question-${index}`}>{question}</li>
                          ))}</ul>
                        </div>
                      ) : null}
                    </section>
                  ) : null}
                  {view.state === "READY_FOR_DECISION" ? (
                    <label className="manual-chatgpt-freeze-ack">
                      <input
                        type="checkbox"
                        checked={freezeAcknowledged}
                        onChange={(event) => setFreezeAcknowledged(event.target.checked)}
                        disabled={busy}
                      />
                      <span>我确认这是研究只读决定；冻结不会授权交易、外部执行或新增模型调用。</span>
                    </label>
                  ) : null}
                  {view.state === "FROZEN" ? (
                    <div className="manual-chatgpt-frozen-selection">
                      <CheckCircle2 size={18} />
                      已冻结：{view.decisionCard.options.find((option) => option.optionId === view.confirmedOptionId)?.title || view.confirmedOptionId}
                    </div>
                  ) : null}
                </section>
              ) : null}
              {finalDecisionState ? (
                <details className="manual-chatgpt-audit-details">
                  <summary>
                    <span className="manual-chatgpt-audit-summary-content">
                      <span>
                        <strong>查看审查记录与角色视角</strong>
                        <small>决定卡是当前主视图；展开后可核对完整来源链。</small>
                      </span>
                      <em>{view.apiReviewRecords.length} 条独立审查 · {view.result?.panels?.length || 0} 个 Panel</em>
                    </span>
                  </summary>
                  <div className="manual-chatgpt-audit-details-body">
                    {apiReviewRecordsSection}
                    {importedPanelsSection}
                  </div>
                </details>
              ) : importedPanelsSection}
            </>
          )}
          {error ? <div className="manual-chatgpt-error" role="alert"><AlertTriangle size={17} />{error}</div> : null}
        </div>

        <footer>
          {view
          && primaryAction.id !== "reset_for_new_bundle"
          && !["BUNDLE_READY", "WAITING_FOR_CHATGPT", "IMPORT_REJECTED", "CONTEXT_STALE"].includes(view.state) ? (
            <button className="secondary" type="button" disabled={busy} onClick={resetForNewBundle}>创建新任务</button>
          ) : <span />}
          {renderPrimaryAction()}
        </footer>
      </section>
    </div>
  );
}
