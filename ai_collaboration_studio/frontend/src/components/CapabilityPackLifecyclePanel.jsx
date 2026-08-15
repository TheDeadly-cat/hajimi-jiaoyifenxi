import { AlertTriangle, History, RotateCcw, ShieldCheck } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
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
import { shortPluginHash } from "../capabilityContributions";

function targetKey(target) {
  return target ? `${target.kind}:${target.id}@${target.version}:${target.targetSha256}` : "";
}

function PreviewImpact({ preview }) {
  const impact = preview.impact;
  return <div className="plugin-lifecycle-impact" aria-label="生命周期影响预览">
    <div className="plugin-lifecycle-impact-state">
      <span><small>当前状态</small><strong>{pluginLifecycleStateLabel(preview.current.runtime_state)}</strong></span>
      <b>→</b>
      <span><small>确认后</small><strong>{pluginLifecycleStateLabel(preview.result.runtime_state)}</strong></span>
    </div>
    <dl>
      <div><dt>受影响房间</dt><dd>{impact.affectedRoomCount} 个</dd></div>
      <div><dt>运行 / 暂停轮次</dt><dd>{impact.runningRoundCount} / {impact.pausedRoundCount}</dd></div>
      <div><dt>历史轮次</dt><dd>{impact.historicalRoundCount} 个</dd></div>
      <div><dt>历史产物</dt><dd>{impact.historicalArtifactCount} 个</dd></div>
    </dl>
    {impact.workspaceLabels.length ? <p>将转为只读的工作区：{impact.workspaceLabels.join("、")}</p> : null}
    {impact.affectedRooms.length ? <details><summary>查看受影响房间</summary><ul>{impact.affectedRooms.map((room) => <li key={room.id}>{room.title}</li>)}</ul></details> : null}
    <p className="plugin-lifecycle-decision-safe"><ShieldCheck size={13} />历史记录不会删除或自动迁移；用户最终决定不受影响。</p>
  </div>;
}

function targetKindLabel(kind) {
  if (kind === "capability_pack") return "能力包";
  if (kind === "domain_adapter") return "领域适配";
  if (kind === "ui_contribution") return "界面组件";
  return "插件组件";
}

function ReplacementDeclarations({ targets }) {
  if (!targets.length) return null;
  return <section className="plugin-lifecycle-replacements" aria-label="替代声明">
    <header>
      <span><strong>替代声明</strong><small>只读</small></span>
      <small>不会自动迁移</small>
    </header>
    <p>这些声明只说明建议接续的精确版本；系统不会自动替换房间、历史记录或用户决定。</p>
    <div>
      {targets.map((target) => {
        const status = target.replacementStatus;
        const replacement = target.replacement;
        const availabilityLabel = !status.integrityOk
          ? "无法核验"
          : status.currentRuntimeAvailable
            ? "当前可用"
            : "当前不可用";
        const statusDetail = !status.integrityOk
          ? "替代版本已声明，但其精确当前状态无法核验，不能据此建立新绑定。"
          : status.currentRuntimeAvailable
            ? "替代版本当前可用；是否采用仍由用户单独决定。"
            : `替代版本当前状态：${pluginLifecycleStateLabel(status.currentRuntimeState)}。`;
        return <article
          className={`plugin-lifecycle-replacement ${status.usable ? "ready" : "unavailable"}`}
          key={`${target.kind}:${target.id}@${target.version}`}
        >
          <div className="plugin-lifecycle-replacement-heading">
            <span><small>{targetKindLabel(target.kind)}</small><strong>{target.label || target.id}</strong></span>
            <em>{availabilityLabel}</em>
          </div>
          <p>{target.id}@{target.version} → {status.targetLabel || replacement.id}@{replacement.version}</p>
          <p>{statusDetail}</p>
          <details className="plugin-lifecycle-technical">
            <summary>精确版本信息</summary>
            <small>来源 {shortPluginHash(target.targetSha256)} · 替代 {shortPluginHash(replacement.sha256)}</small>
          </details>
        </article>;
      })}
    </div>
  </section>;
}

export function CapabilityPackLifecyclePanel({
  pluginLifecycle,
  onPreview,
  onTransition,
}) {
  const view = useMemo(
    () => pluginLifecycleCatalogView(pluginLifecycle),
    [pluginLifecycle],
  );
  const [review, setReview] = useState(null);
  const [reason, setReason] = useState("");
  const [historyConfirmed, setHistoryConfirmed] = useState(false);
  const [migrationConfirmed, setMigrationConfirmed] = useState(false);
  const [tombstoneConfirmation, setTombstoneConfirmation] = useState("");
  const [notice, setNotice] = useState("");
  const requestRef = useRef({ sequence: 0, controller: null });

  useEffect(() => {
    setReview(null);
    setReason("");
    setHistoryConfirmed(false);
    setMigrationConfirmed(false);
    setTombstoneConfirmation("");
    requestRef.current.controller?.abort();
    requestRef.current = { sequence: requestRef.current.sequence + 1, controller: null };
  }, [view.viewSha256]);

  useEffect(() => () => requestRef.current.controller?.abort(), []);

  const resetReview = () => {
    requestRef.current.controller?.abort();
    requestRef.current = { sequence: requestRef.current.sequence + 1, controller: null };
    setReview(null);
    setReason("");
    setHistoryConfirmed(false);
    setMigrationConfirmed(false);
    setTombstoneConfirmation("");
  };

  const previewAction = async (target, action) => {
    if (!view.integrityOk || review?.busy) return;
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
      const preview = pluginLifecycleImpactPreviewView(data.preview, { target, action });
      if (!preview.integrityOk) throw new Error(preview.errors[0] || "影响预览无法验证。");
      setReview({
        target,
        action,
        preview,
        busy: false,
        error: "",
        clientRequestId: newPluginLifecycleClientRequestId(),
      });
    } catch (error) {
      if (error?.name === "AbortError" || requestRef.current.sequence !== sequence) return;
      setReview({
        target,
        action,
        preview: null,
        busy: false,
        error: error.message || "影响预览失败。",
        clientRequestId: "",
      });
    }
  };

  const submitTransition = async () => {
    if (!review?.preview || review.busy) return;
    const cleanReason = reason.trim();
    if (cleanReason.length < 4) {
      setReview((current) => ({ ...current, error: "请填写至少 4 个字符的变更原因。" }));
      return;
    }
    if (!historyConfirmed || !migrationConfirmed) {
      setReview((current) => ({ ...current, error: "请确认历史保留和不自动迁移两项边界。" }));
      return;
    }
    if (review.action === "tombstone" && tombstoneConfirmation.trim() !== review.target.label) {
      setReview((current) => ({ ...current, error: `请输入能力包名称“${review.target.label}”完成永久停用确认。` }));
      return;
    }
    setReview((current) => ({ ...current, busy: true, error: "" }));
    try {
      const payload = buildPluginLifecycleTransitionRequest({
        target: review.target,
        action: review.action,
        preview: review.preview,
        clientRequestId: review.clientRequestId,
        reason: cleanReason,
      });
      const data = await onTransition(payload);
      const result = pluginLifecycleTransitionResultView(data, {
        target: review.target,
        action: review.action,
      });
      if (!result.integrityOk) throw new Error(result.errors[0] || "提交结果无法验证。");
      setNotice(`${review.target.label}：${pluginLifecycleActionLabel(review.action)}已记录。`);
      resetReview();
    } catch (error) {
      setReview((current) => ({
        ...current,
        busy: false,
        error: error.message || "生命周期变更失败。",
      }));
    }
  };

  if (!view.integrityOk) {
    return <section className="plugin-lifecycle-panel integrity-failed" aria-label="能力包状态异常">
      <header><span><AlertTriangle size={15} /><strong>能力包状态无法验证</strong></span></header>
      <p>所有生命周期动作和新绑定均已关闭。{view.errors[0] || "请刷新后重试。"}</p>
    </section>;
  }

  return <section className="plugin-lifecycle-panel" aria-label="能力包生命周期管理">
    <header>
      <span><History size={15} /><strong>能力包状态管理</strong></span>
      <small>影响所有房间</small>
    </header>
    <p>这里管理能力包能否继续用于新房间和插件操作；不会删除历史记录，也不会改变用户最终决定。</p>
    {notice ? <div className="plugin-lifecycle-notice" role="status"><ShieldCheck size={13} />{notice}</div> : null}
    <div className="plugin-lifecycle-pack-list">
      {view.capabilityPacks.map((pack) => {
        const target = pack.lifecycle;
        const selectedReview = targetKey(review?.target) === targetKey(target) ? review : null;
        return <article className={`plugin-lifecycle-pack ${target?.runtimeState || "unknown"}`} key={`${pack.id}@${pack.pack_version}`}>
          <div className="plugin-lifecycle-pack-heading">
            <span><strong>{pack.name || pack.id}</strong><small>{pack.description}</small></span>
            <em>{target?.systemManaged ? "内核管理" : pluginLifecycleStateLabel(target)}</em>
          </div>
          <p>{target?.systemManaged
            ? "群聊内核协议不能通过插件生命周期停用。"
            : target?.runtimeAvailable
              ? "当前可用于新绑定和插件操作。"
              : pluginLifecycleRuntimeReason(target)}</p>
          <details className="plugin-lifecycle-technical"><summary>版本信息</summary><small>{pack.id}@{pack.pack_version} · {shortPluginHash(pack.manifest_sha256)} · head {target?.headSequence ?? "?"}</small></details>
          {target?.availableActions?.length ? <div className="plugin-lifecycle-actions">
            {target.availableActions.map((action) => <button
              className={action === "tombstone" || action === "quarantine" ? "secondary danger" : "secondary"}
              disabled={Boolean(review?.busy)}
              key={action}
              onClick={() => previewAction(target, action)}
              type="button"
            >{action === "enable" || action === "clear_quarantine" || action === "reinstate" ? <RotateCcw size={12} /> : null}{pluginLifecycleActionLabel(action)}</button>)}
          </div> : null}
          {selectedReview ? <section className="plugin-lifecycle-review" aria-label={`${target.label} 影响确认`}>
            <header><strong>{pluginLifecycleActionLabel(selectedReview.action)}</strong><button type="button" className="text-action" disabled={selectedReview.busy} onClick={resetReview}>取消</button></header>
            {selectedReview.busy && !selectedReview.preview ? <p>正在读取服务端影响范围…</p> : null}
            {selectedReview.preview ? <PreviewImpact preview={selectedReview.preview} /> : null}
            {selectedReview.preview ? <>
              <label>变更原因<textarea minLength={4} maxLength={500} value={reason} onChange={(event) => setReason(event.target.value)} placeholder="说明为什么现在需要变更此能力包状态。" /></label>
              <label className="checkbox-line"><input type="checkbox" checked={historyConfirmed} onChange={(event) => setHistoryConfirmed(event.target.checked)} />我确认历史记录将只读保留，不会删除。</label>
              <label className="checkbox-line"><input type="checkbox" checked={migrationConfirmed} onChange={(event) => setMigrationConfirmed(event.target.checked)} />我确认系统不会自动迁移或替换能力包。</label>
              {selectedReview.action === "tombstone" ? <label className="plugin-lifecycle-tombstone-confirm">永久停用确认<input value={tombstoneConfirmation} onChange={(event) => setTombstoneConfirmation(event.target.value)} placeholder={`请输入：${target.label}`} /><small>墓碑状态不可恢复；历史内容仍会保留。</small></label> : null}
              <button className={selectedReview.action === "tombstone" ? "primary danger" : "primary"} disabled={selectedReview.busy} onClick={submitTransition} type="button">
                {selectedReview.busy ? "正在提交…" : `确认${pluginLifecycleActionLabel(selectedReview.action)}`}
              </button>
            </> : null}
            {selectedReview.error ? <p className="plugin-lifecycle-error" role="alert">{selectedReview.error}</p> : null}
          </section> : null}
        </article>;
      })}
    </div>
    <ReplacementDeclarations targets={view.replacementDeclarations} />
  </section>;
}
