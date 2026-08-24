import { AlertTriangle, ChevronDown, History, RotateCcw, ShieldCheck } from "lucide-react";
import { memo, useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import {
  buildPluginLifecyclePreviewRequest,
  buildPluginLifecycleTransitionRequest,
  newPluginLifecycleClientRequestId,
  pluginLifecycleActionLabel,
  pluginLifecycleCatalogView,
  pluginLifecycleImpactPreviewView,
  pluginLifecycleRuntimeReason,
  pluginLifecycleStateLabel,
  pluginLifecycleTransitionResultView,
} from "../pluginLifecycle";
import {
  pluginLifecycleActionPresentation,
  pluginLifecycleCatalogPresentation,
  pluginLifecycleReviewControl,
  pluginLifecycleTargetKey,
} from "../pluginLifecycleUi";
import { shortPluginHash } from "../capabilityContributions";
import "../styles/plugin-lifecycle.css";

function requestErrorMessage(error, fallback) {
  return error instanceof Error && error.message.trim()
    ? error.message.trim().slice(0, 1000)
    : fallback;
}

const PreviewImpact = memo(function PreviewImpact({ preview }) {
  const impact = preview.impact;
  return <section className="plugin-lifecycle-impact" aria-label="生命周期影响预览">
    <div className="plugin-lifecycle-impact-heading"><span>SEALED IMPACT</span><small>预览 {shortPluginHash(preview.previewSha256)}</small></div>
    <div className="plugin-lifecycle-impact-state">
      <span><small>当前状态</small><strong>{pluginLifecycleStateLabel(preview.current.runtime_state)}</strong></span>
      <b aria-hidden="true">→</b>
      <span><small>确认后</small><strong>{pluginLifecycleStateLabel(preview.result.runtime_state)}</strong></span>
    </div>
    <dl>
      <div><dt>受影响房间</dt><dd>{impact.affectedRoomCount} 个</dd></div>
      <div><dt>运行 / 暂停</dt><dd>{impact.runningRoundCount} / {impact.pausedRoundCount}</dd></div>
      <div><dt>历史轮次</dt><dd>{impact.historicalRoundCount} 个</dd></div>
      <div><dt>历史产物</dt><dd>{impact.historicalArtifactCount} 个</dd></div>
    </dl>
    {impact.workspaceLabels.length ? <p>转为只读的工作区：{impact.workspaceLabels.join("、")}</p> : null}
    {impact.affectedRooms.length ? <details><summary>查看受影响房间</summary><ul>{impact.affectedRooms.map((room) => <li key={room.id}>{room.title}</li>)}</ul></details> : null}
    <p className="plugin-lifecycle-decision-safe"><ShieldCheck aria-hidden="true" size={13} />历史记录不会删除或自动迁移；用户最终决定不受影响。</p>
  </section>;
});

function targetKindLabel(kind) {
  if (kind === "capability_pack") return "能力包";
  if (kind === "domain_adapter") return "领域适配";
  if (kind === "ui_contribution") return "界面组件";
  return "插件组件";
}


const CapabilityPackCard = memo(function CapabilityPackCard({
  pack,
  index,
  selected,
  actionsDisabled,
  onPreviewAction,
}) {
  const target = pack.lifecycle;
  const displayName = pack.name || pack.id;
  const disclosureId = useId();
  const headingId = useId();
  const actionCount = target?.availableActions?.length || 0;
  const attentionRequired = !target?.systemManaged && target?.runtimeAvailable !== true;
  const [expanded, setExpanded] = useState(attentionRequired || selected);

  useEffect(() => {
    if (selected || attentionRequired) setExpanded(true);
  }, [attentionRequired, selected]);

  return (
    <article
      aria-labelledby={headingId}
      className={`plugin-lifecycle-pack ${target?.runtimeState || "unknown"} ${selected ? "selected" : ""} ${expanded ? "expanded" : "collapsed"}`}
      data-runtime-state={target?.runtimeState || "unknown"}
      role="listitem"
    >
      <div className="plugin-lifecycle-pack-index" aria-hidden="true">{String(index + 1).padStart(2, "0")}</div>
      <div className="plugin-lifecycle-pack-summary">
        <div className="plugin-lifecycle-pack-heading">
          <span>
            <small>{targetKindLabel(target?.kind)}</small>
            <h5 id={headingId}>{displayName}</h5>
            <span>{pack.description}</span>
          </span>
          <em>{target?.systemManaged ? "内核管理" : pluginLifecycleStateLabel(target)}</em>
        </div>
        <div className="plugin-lifecycle-pack-contract" aria-label={`${displayName} 合同摘要`}>
          <span><small>精确版本</small><strong>{pack.id}@{pack.pack_version}</strong></span>
          <span><small>可审阅动作</small><strong>{target?.systemManaged ? "内核管理" : `${actionCount} 项`}</strong></span>
        </div>
        <p className="plugin-lifecycle-runtime-reason">
          {target?.systemManaged
            ? "群聊内核协议不能通过插件生命周期停用。"
            : target?.runtimeAvailable
              ? "当前可用于新绑定和插件操作。"
              : pluginLifecycleRuntimeReason(target)}
        </p>
        <button
          aria-controls={disclosureId}
          aria-expanded={expanded}
          aria-label={`${expanded ? "收起" : "展开"}${displayName}精确合同与动作`}
          className="plugin-lifecycle-disclosure"
          onClick={() => setExpanded((current) => !current)}
          type="button"
        >
          <span>{expanded ? "收起合同与动作" : "展开合同与动作"}</span>
          <ChevronDown aria-hidden="true" size={15} />
        </button>
      </div>
      <div
        aria-labelledby={headingId}
        className="plugin-lifecycle-pack-details"
        hidden={!expanded}
        id={disclosureId}
        role="region"
      >
        <dl className="plugin-lifecycle-identity">
          <div><dt>精确版本</dt><dd>{pack.id}@{pack.pack_version}</dd></div>
          <div><dt>来源封印</dt><dd>{shortPluginHash(pack.manifest_sha256)}</dd></div>
          <div><dt>生命周期 head</dt><dd>{target?.headSequence ?? "?"} / {shortPluginHash(target?.headSha256)}</dd></div>
        </dl>
        {target?.availableActions?.length ? (
          <div className="plugin-lifecycle-actions" aria-label={`${displayName} 可用生命周期动作`}>
            {target.availableActions.map((action) => (
              <button
                className={action === "tombstone" || action === "quarantine" ? "secondary danger" : "secondary"}
                disabled={actionsDisabled}
                key={action}
                onClick={() => onPreviewAction(target, action)}
                type="button"
              >
                {action === "enable" || action === "clear_quarantine" || action === "reinstate"
                  ? <RotateCcw aria-hidden="true" size={12} />
                  : null}
                {pluginLifecycleActionLabel(action)}
              </button>
            ))}
          </div>
        ) : <p className="plugin-lifecycle-no-actions">此精确版本没有可用的生命周期动作。</p>}
      </div>
    </article>
  );
});


const ReplacementDeclarations = memo(function ReplacementDeclarations({ targets }) {
  if (!targets.length) return null;
  return <section className="plugin-lifecycle-replacements" aria-label="替代声明">
    <header><span><strong>替代声明</strong><small>只读参考</small></span><small>NO AUTO MIGRATION</small></header>
    <p>声明只指向建议接续的精确版本；系统不会自动替换房间、历史记录或用户决定。</p>
    <div className="plugin-lifecycle-replacement-grid">
      {targets.map((target) => {
        const status = target.replacementStatus;
        const replacement = target.replacement;
        const availabilityLabel = !status.integrityOk ? "无法核验" : status.currentRuntimeAvailable ? "可供新绑定" : "当前不可用";
        const statusDetail = !status.integrityOk
          ? "替代版本已声明，但其精确当前状态无法核验，不能据此建立新绑定。"
          : status.currentRuntimeAvailable
            ? "替代版本当前可用；是否采用仍由用户单独决定。"
            : `替代版本当前状态：${pluginLifecycleStateLabel(status.currentRuntimeState)}。`;
        return <article className={`plugin-lifecycle-replacement ${status.usable ? "ready" : "unavailable"}`} key={pluginLifecycleTargetKey(target)}>
          <div className="plugin-lifecycle-replacement-heading"><span><small>{targetKindLabel(target.kind)}</small><strong>{target.label || target.id}</strong></span><em>{availabilityLabel}</em></div>
          <p>{target.id}@{target.version} → {status.targetLabel || replacement.id}@{replacement.version}</p>
          <p>{statusDetail}</p>
          <details className="plugin-lifecycle-technical"><summary>精确版本信息</summary><small>来源 {shortPluginHash(target.targetSha256)} · 替代 {shortPluginHash(replacement.sha256)}</small></details>
        </article>;
      })}
    </div>
  </section>;
});

export const CapabilityPackLifecyclePanel = memo(function CapabilityPackLifecyclePanel({ pluginLifecycle, onPreview, onTransition }) {
  const titleId = useId();
  const catalogTitleId = useId();
  const reviewInstructionId = useId();
  const view = useMemo(() => pluginLifecycleCatalogView(pluginLifecycle), [pluginLifecycle]);
  const catalog = useMemo(() => pluginLifecycleCatalogPresentation(view), [view]);
  const [review, setReview] = useState(null);
  const [reason, setReason] = useState("");
  const [historyConfirmed, setHistoryConfirmed] = useState(false);
  const [migrationConfirmed, setMigrationConfirmed] = useState(false);
  const [tombstoneConfirmation, setTombstoneConfirmation] = useState("");
  const [notice, setNotice] = useState("");
  const requestRef = useRef({ sequence: 0, controller: null });
  const reviewHeadingRef = useRef(null);

  useEffect(() => {
    setReview(null);
    setReason("");
    setHistoryConfirmed(false);
    setMigrationConfirmed(false);
    setTombstoneConfirmation("");
    setNotice("");
    requestRef.current.controller?.abort();
    requestRef.current = { sequence: requestRef.current.sequence + 1, controller: null };
  }, [view.viewSha256]);

  useEffect(() => () => requestRef.current.controller?.abort(), []);
  useEffect(() => {
    if (review && !review.busy) reviewHeadingRef.current?.focus();
  }, [review?.action, review?.busy, review?.clientRequestId]);

  const resetReview = useCallback(() => {
    requestRef.current.controller?.abort();
    requestRef.current = { sequence: requestRef.current.sequence + 1, controller: null };
    setReview(null);
    setReason("");
    setHistoryConfirmed(false);
    setMigrationConfirmed(false);
    setTombstoneConfirmation("");
  }, []);

  const previewAction = useCallback(async (target, action) => {
    if (!view.integrityOk || review?.busy) return;
    if (typeof onPreview !== "function") {
      setNotice("当前环境未提供生命周期影响预览入口，所有变更保持关闭。");
      return;
    }
    const controller = new AbortController();
    requestRef.current.controller?.abort();
    const sequence = requestRef.current.sequence + 1;
    requestRef.current = { sequence, controller };
    setNotice("");
    setReason("");
    setHistoryConfirmed(false);
    setMigrationConfirmed(false);
    setTombstoneConfirmation("");
    setReview({ target, action, preview: null, busy: true, error: "", clientRequestId: "" });
    try {
      const data = await onPreview(buildPluginLifecyclePreviewRequest(target, action), controller.signal);
      if (requestRef.current.sequence !== sequence) return;
      const preview = pluginLifecycleImpactPreviewView(data?.preview, { target, action });
      if (!preview.integrityOk) throw new Error(preview.errors[0] || "影响预览无法验证。");
      requestRef.current = { sequence, controller: null };
      setReview({ target, action, preview, busy: false, error: "", clientRequestId: newPluginLifecycleClientRequestId() });
    } catch (error) {
      if (error?.name === "AbortError" || requestRef.current.sequence !== sequence) return;
      requestRef.current = { sequence, controller: null };
      setReview({ target, action, preview: null, busy: false, error: requestErrorMessage(error, "影响预览失败。"), clientRequestId: "" });
    }
  }, [onPreview, review?.busy, view.integrityOk]);

  const reviewControl = useMemo(
    () => pluginLifecycleReviewControl({
      review,
      reason,
      historyConfirmed,
      migrationConfirmed,
      tombstoneConfirmation,
    }),
    [historyConfirmed, migrationConfirmed, reason, review, tombstoneConfirmation],
  );

  const submitTransition = async () => {
    if (!review || review.busy) return;
    if (!reviewControl.canSubmit) {
      setReview((current) => current ? { ...current, error: `提交许可未完成：${reviewControl.instruction}` } : current);
      return;
    }
    if (typeof onTransition !== "function") {
      setReview((current) => current ? { ...current, error: "当前环境未提供生命周期变更入口。" } : current);
      return;
    }
    const submittingReview = review;
    requestRef.current.controller?.abort();
    const sequence = requestRef.current.sequence + 1;
    requestRef.current = { sequence, controller: null };
    setReview((current) => current ? { ...current, busy: true, error: "" } : current);
    try {
      const payload = buildPluginLifecycleTransitionRequest({
        target: submittingReview.target,
        action: submittingReview.action,
        preview: submittingReview.preview,
        clientRequestId: submittingReview.clientRequestId,
        reason,
      });
      const data = await onTransition(payload);
      if (requestRef.current.sequence !== sequence) return;
      const result = pluginLifecycleTransitionResultView(data, { target: submittingReview.target, action: submittingReview.action });
      if (!result.integrityOk) throw new Error(result.errors[0] || "提交结果无法验证。");
      setNotice(`${submittingReview.target.label}：${pluginLifecycleActionLabel(submittingReview.action)}已记录。`);
      resetReview();
    } catch (error) {
      if (requestRef.current.sequence !== sequence) return;
      setReview((current) => current ? { ...current, busy: false, error: requestErrorMessage(error, "生命周期变更失败。") } : current);
    }
  };

  if (!view.integrityOk) {
    return <section className="plugin-lifecycle-panel integrity-failed" aria-label="能力包状态异常">
      <header><span><AlertTriangle aria-hidden="true" size={15} /><strong>能力包状态无法验证</strong></span></header>
      <p>所有生命周期动作和新绑定均已关闭。{view.errors?.[0] || "请刷新后重试。"}</p>
    </section>;
  }

  const actionPresentation = useMemo(
    () => pluginLifecycleActionPresentation(review?.action),
    [review?.action],
  );
  const selectedTargetKey = useMemo(
    () => pluginLifecycleTargetKey(review?.target),
    [review?.target],
  );
  const actionHandlersAvailable = typeof onPreview === "function" && typeof onTransition === "function";

  return <section className="plugin-lifecycle-panel" aria-labelledby={titleId} aria-busy={Boolean(review?.busy)}>
    <header className="plugin-lifecycle-masthead">
      <div className="plugin-lifecycle-masthead-mark"><History aria-hidden="true" size={18} /><span>GLOBAL<br />CHANGE CONTROL</span></div>
      <div><small>PLUGIN GOVERNANCE / EXACT VERSION</small><h3 id={titleId}>能力包生命周期控制台</h3><p>先冻结影响，再签署变更。这里只控制新绑定与插件动作，不删除历史，也不替代用户决定。</p></div>
      <span className="plugin-lifecycle-scope">全房间范围</span>
    </header>
    <dl className="plugin-lifecycle-stats" aria-label="生命周期目录摘要">
      <div><dt>目录条目</dt><dd>{catalog.total}</dd></div><div><dt>新绑定可用</dt><dd>{catalog.ready}</dd></div><div><dt>受限版本</dt><dd>{catalog.restricted}</dd></div><div><dt>可审阅动作</dt><dd>{catalog.actionable}</dd></div><div><dt>内核管理</dt><dd>{catalog.systemManaged}</dd></div>
    </dl>
    {notice ? <div className="plugin-lifecycle-notice" role="status"><ShieldCheck aria-hidden="true" size={13} />{notice}</div> : null}
    {!actionHandlersAvailable ? <p className="plugin-lifecycle-offline-note">当前运行环境未提供完整生命周期处理入口；目录保持只读，动作按钮已关闭。</p> : null}
    <section className="plugin-lifecycle-directory" aria-labelledby={catalogTitleId}>
      <header className="plugin-lifecycle-directory-bar">
        <span>
          <small>REVIEW DIRECTORY / EXACT TARGETS</small>
          <h4 id={catalogTitleId}>精确版本目录</h4>
          <p>先展开精确合同，再选择生命周期动作；受限版本会自动展开。</p>
        </span>
        <data value={catalog.actionable}>{catalog.actionable ? `${catalog.actionable} 项可审阅` : "当前无可审阅动作"}</data>
      </header>
      <div className="plugin-lifecycle-pack-list" role="list" aria-labelledby={catalogTitleId}>
        {view.capabilityPacks.map((pack, index) => {
          const target = pack.lifecycle;
          const isSelected = selectedTargetKey === pluginLifecycleTargetKey(target);
          return (
            <CapabilityPackCard
              actionsDisabled={Boolean(review?.busy) || !actionHandlersAvailable}
              index={index}
              key={pluginLifecycleTargetKey(target)}
              onPreviewAction={previewAction}
              pack={pack}
              selected={isSelected}
            />
          );
        })}
      </div>
    </section>
    {review ? <section aria-busy={review.busy} aria-label={`${review.target.label} 变更控制`} className="plugin-lifecycle-review" data-action={review.action} data-phase={reviewControl.phase}>
      <header className="plugin-lifecycle-review-heading"><div className="plugin-lifecycle-review-seal" aria-hidden="true"><ShieldCheck size={20} /></div><div><small>{actionPresentation.eyebrow} / CHANGE PERMIT</small><h4 ref={reviewHeadingRef} tabIndex={-1}>{review.target.label} · {pluginLifecycleActionLabel(review.action)}</h4><p>{actionPresentation.summary}</p></div><button type="button" className="text-action" disabled={review.busy} onClick={resetReview}>取消审阅</button></header>
      {review.busy && !review.preview ? <p className="plugin-lifecycle-loading" role="status">正在读取服务端影响范围并冻结精确目标…</p> : null}
      {review.preview ? <PreviewImpact preview={review.preview} /> : null}
      {review.preview ? <div className="plugin-lifecycle-permit-layout">
        <div className="plugin-lifecycle-permit-form">
          <label className="plugin-lifecycle-reason">变更原因<textarea autoComplete="off" disabled={review.busy} maxLength={500} minLength={4} onChange={(event) => setReason(event.target.value)} placeholder="说明为什么现在需要变更此精确版本的生命周期状态。" value={reason} /><small>{reviewControl.reasonLength} / 500（按去除首尾空白后的内容计）</small></label>
          <label className="checkbox-line"><input disabled={review.busy} type="checkbox" checked={historyConfirmed} onChange={(event) => setHistoryConfirmed(event.target.checked)} />我确认历史记录将只读保留，不会删除。</label>
          <label className="checkbox-line"><input disabled={review.busy} type="checkbox" checked={migrationConfirmed} onChange={(event) => setMigrationConfirmed(event.target.checked)} />我确认系统不会自动迁移或替换能力包。</label>
          {reviewControl.tombstoneRequired ? <label className="plugin-lifecycle-tombstone-confirm">永久停用确认<input disabled={review.busy} value={tombstoneConfirmation} onChange={(event) => setTombstoneConfirmation(event.target.value)} placeholder={`请输入：${review.target.label}`} /><small>墓碑状态不可恢复；历史内容仍会保留。</small></label> : null}
        </div>
        <aside className="plugin-lifecycle-permit" aria-label="提交许可清单"><header><span>PERMIT CHECK</span><strong>{reviewControl.permitChecks.filter((check) => check.passed).length}/{reviewControl.permitChecks.length}</strong></header><ul>{reviewControl.permitChecks.map((check) => <li className={check.passed ? "passed" : "pending"} key={check.id}><span aria-hidden="true">{check.passed ? "✓" : "·"}</span>{check.label}</li>)}</ul><p id={reviewInstructionId}>{reviewControl.instruction}</p><button aria-describedby={reviewInstructionId} className={review.action === "tombstone" ? "primary danger" : "primary"} disabled={!reviewControl.canSubmit || typeof onTransition !== "function"} onClick={submitTransition} type="button">{review.busy ? "正在提交…" : `确认${pluginLifecycleActionLabel(review.action)}`}</button></aside>
      </div> : null}
      {review.error ? <p className="plugin-lifecycle-error" role="alert"><AlertTriangle aria-hidden="true" size={14} />{review.error}</p> : null}
    </section> : null}
    <ReplacementDeclarations targets={view.replacementDeclarations} />
  </section>;
});
