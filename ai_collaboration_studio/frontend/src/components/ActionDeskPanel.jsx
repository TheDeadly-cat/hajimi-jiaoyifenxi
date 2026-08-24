import {
  AlertTriangle,
  CheckCircle2,
  ClipboardList,
  GitBranch,
  Link2,
  LoaderCircle,
  MessageSquareText,
  Save,
  ShieldCheck,
} from "lucide-react";
import { memo, useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import {
  ACTION_DESK_STATE_LABELS,
  ACTION_DESK_STATES,
  actionDeskComposerText,
  buildActionDeskTransitionRequest,
  newActionDeskClientRequestId,
  normalizeActionDeskResponse,
} from "../actionDesk";
import {
  buildActionContinuationRequest,
  continuationTargetCandidates,
  newActionContinuationClientRequestId,
  normalizeActionDeskContinuationsResponse,
} from "../actionContinuation";
import "../styles/action-desk-polish.css";

const EMPTY_LOAD_STATE = Object.freeze({ status: "idle", desk: null, error: "" });
const EMPTY_MUTATION_STATE = Object.freeze({ status: "idle", sourceKey: "", error: "" });
const EMPTY_CONTINUATION_STATE = Object.freeze({ status: "idle", view: null, error: "" });
const ACTION_DESK_TIMESTAMP_FORMATTER = new Intl.DateTimeFormat("zh-CN", {
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
});

function shortHash(value) {
  const hash = String(value || "").trim();
  return hash.length === 64 ? `${hash.slice(0, 8)}…${hash.slice(-6)}` : "未封印";
}

function formatTimestamp(value) {
  const timestamp = Number(value);
  if (!Number.isFinite(timestamp) || !Number.isFinite(new Date(timestamp).getTime())) return "时间未核验";
  return ACTION_DESK_TIMESTAMP_FORMATTER.format(new Date(timestamp));
}

function formFromRow(row) {
  return {
    owner: row.owner || "",
    due: row.due || "",
    state: row.state || "open",
    note: row.note || "",
    clientRequestId: newActionDeskClientRequestId(),
  };
}

function formsFromRows(rows) {
  return Object.fromEntries(
    rows.filter((row) => row.valid && row.sourceKey).map((row) => [row.sourceKey, formFromRow(row)]),
  );
}

function continuationFormFromRow(row, candidates = []) {
  return {
    targetKey: candidates[0]?.sourceKey || "",
    reason: "",
    clientRequestId: newActionContinuationClientRequestId(),
  };
}

function ActionSource({ row }) {
  return (
    <div className="action-desk-source">
      <span>
        <strong>{row.artifactTitle} · v{row.artifactVersion}</strong>
        <small>精确待办来源 · {row.actionId}</small>
      </span>
      <code title={row.actionSnapshotSha256}>{shortHash(row.actionSnapshotSha256)}</code>
    </div>
  );
}

function RedactedRow({ kind }) {
  return (
    <article className="action-desk-row integrity-failed" role="alert">
      <AlertTriangle aria-hidden="true" size={16} />
      <span>
        <strong>{kind === "candidate" ? "候选待办来源损坏" : "行动项完整性无法确认"}</strong>
        <small>来源、状态和可编辑字段已隐藏；不会用当前产物或相邻条目替代。</small>
      </span>
    </article>
  );
}

function CandidateCard({ row, form, locked, saving, onChange, onAdopt }) {
  return (
    <article className="action-desk-row candidate">
      <ActionSource row={row} />
      <p className="action-desk-text">{row.text}</p>
      <div className="action-desk-row-meta">
        <span>产物状态：{ACTION_DESK_STATE_LABELS[row.state]}</span>
        <span>证据关系：{row.evidenceCount}</span>
        <em>尚未采纳</em>
      </div>
      <div className="action-desk-form-grid candidate-fields">
        <label>
          负责人
          <input
            value={form.owner}
            onChange={(event) => onChange("owner", event.target.value)}
            placeholder="待分配"
            disabled={locked}
          />
        </label>
        <label>
          期限 / 里程碑
          <input
            value={form.due}
            onChange={(event) => onChange("due", event.target.value)}
            placeholder="可留空"
            disabled={locked}
          />
        </label>
      </div>
      <div className="action-desk-actions">
        <button className="primary compact" type="button" onClick={onAdopt} disabled={locked} aria-busy={saving}>
          {saving ? <LoaderCircle aria-hidden="true" className="spin" size={14} /> : <CheckCircle2 aria-hidden="true" size={14} />}
          {saving ? "正在采纳…" : "采纳到行动台"}
        </button>
      </div>
    </article>
  );
}

function ContinuationCard({ row, relation, candidates, form, locked, saving, onChange, onSubmit }) {
  if (relation?.valid) {
    return (
      <div className="action-desk-continuation linked" role="status">
        <span className="action-desk-continuation-heading"><Link2 aria-hidden="true" size={13} /><strong>已建立显式延续</strong></span>
        <small>旧版 v{relation.source.artifactVersion} · {relation.source.actionId} → 新版 v{relation.target.artifactVersion} · {relation.target.actionId}</small>
        {relation.reason ? <small>用户说明：{relation.reason}</small> : null}
        <em>旧行动状态不会自动转移，新行动仍需单独采纳。</em>
      </div>
    );
  }
  if (!candidates.length) {
    return (
      <div className="action-desk-continuation unavailable" role="note">
        <span className="action-desk-continuation-heading"><GitBranch aria-hidden="true" size={13} /><strong>可建立新版延续</strong></span>
        <small>当前没有同一产物谱系中更高版本的确认待办候选。</small>
      </div>
    );
  }
  return (
    <div className="action-desk-continuation form" aria-label="建立新版行动延续">
      <span className="action-desk-continuation-heading"><GitBranch aria-hidden="true" size={13} /><strong>旧版行动 → 新版行动</strong></span>
      <small>只建立来源关系；不会采纳新版、复制状态或修改旧行动。</small>
      <label>
        选择新版确认待办
        <select value={form.targetKey} onChange={(event) => onChange("targetKey", event.target.value)} disabled={locked}>
          {candidates.map((candidate) => (
            <option key={candidate.sourceKey} value={candidate.sourceKey}>
              v{candidate.artifactVersion} · {candidate.actionId} · {candidate.text.slice(0, 70)}
            </option>
          ))}
        </select>
      </label>
      <label>
        延续说明（可选）
        <textarea value={form.reason} onChange={(event) => onChange("reason", event.target.value)} placeholder="说明为什么认为新版是同一后续事项" disabled={locked} />
      </label>
      <button className="secondary compact" type="button" onClick={onSubmit} disabled={locked || !form.targetKey} aria-busy={saving}>
        {saving ? <LoaderCircle aria-hidden="true" className="spin" size={13} /> : <Link2 aria-hidden="true" size={13} />}
        {saving ? "正在确认…" : "确认建立延续关系"}
      </button>
    </div>
  );
}

function ItemCard({ row, form, locked, saving, onChange, onSave, onFillComposer, continuation }) {
  return (
    <article className={`action-desk-row item state-${row.state}`}>
      <ActionSource row={row} />
      <p className="action-desk-text">{row.text}</p>
      <div className="action-desk-row-meta">
        <span>修订 v{row.revision}</span>
        <span>更新于 {formatTimestamp(row.updatedAt)}</span>
        <em className={row.sourceCurrent ? "current" : "historical"}>
          {row.sourceCurrent ? "当前确认版本" : `历史确认版本 · 当前 v${row.currentArtifactVersion}`}
        </em>
      </div>
      {continuation ? (
        <ContinuationCard
          row={row}
          relation={continuation.relation}
          candidates={continuation.candidates}
          form={continuation.form}
          locked={locked}
          saving={continuation.saving}
          onChange={continuation.onChange}
          onSubmit={continuation.onSubmit}
        />
      ) : null}
      <div className="action-desk-form-grid">
        <label>
          负责人
          <input
            value={form.owner}
            onChange={(event) => onChange("owner", event.target.value)}
            placeholder="待分配"
            disabled={locked}
          />
        </label>
        <label>
          期限 / 里程碑
          <input
            value={form.due}
            onChange={(event) => onChange("due", event.target.value)}
            placeholder="可留空"
            disabled={locked}
          />
        </label>
        <label>
          状态
          <select value={form.state} onChange={(event) => onChange("state", event.target.value)} disabled={locked}>
            {ACTION_DESK_STATES.map((state) => (
              <option value={state} key={state}>{ACTION_DESK_STATE_LABELS[state]}</option>
            ))}
          </select>
        </label>
        <label className="action-desk-note-field">
          进展说明
          <textarea
            value={form.note}
            onChange={(event) => onChange("note", event.target.value)}
            placeholder="记录进展、阻断原因或完成说明"
            disabled={locked}
          />
        </label>
      </div>
      <div className="action-desk-actions">
        <button className="secondary compact" type="button" onClick={onFillComposer} disabled={locked}>
          <MessageSquareText aria-hidden="true" size={14} />填入讨论框
        </button>
        <button className="primary compact" type="button" onClick={onSave} disabled={locked} aria-busy={saving}>
          {saving ? <LoaderCircle aria-hidden="true" className="spin" size={14} /> : <Save aria-hidden="true" size={14} />}
          {saving ? "正在保存…" : "保存行动项"}
        </button>
      </div>
    </article>
  );
}

export const ActionDeskPanel = memo(function ActionDeskPanel({ roomId, artifactFingerprint = "", onFillComposer }) {
  const normalizedRoomId = String(roomId || "").trim();
  const normalizedArtifactFingerprint = String(artifactFingerprint || "");
  const activeRoomIdRef = useRef(normalizedRoomId);
  activeRoomIdRef.current = normalizedRoomId;
  const requestRef = useRef({ sequence: 0, controller: null });
  const continuationRequestRef = useRef({ sequence: 0, controller: null });
  const mutationControllerRef = useRef(null);
  const [loadState, setLoadState] = useState(EMPTY_LOAD_STATE);
  const [continuationState, setContinuationState] = useState(EMPTY_CONTINUATION_STATE);
  const [mutation, setMutation] = useState(EMPTY_MUTATION_STATE);
  const [candidateForms, setCandidateForms] = useState({});
  const [itemForms, setItemForms] = useState({});
  const [continuationForms, setContinuationForms] = useState({});

  const loadDesk = useCallback(async () => {
    const targetRoomId = normalizedRoomId;
    const previous = requestRef.current;
    previous.controller?.abort();
    if (!targetRoomId) {
      requestRef.current = { sequence: previous.sequence + 1, controller: null };
      setLoadState(EMPTY_LOAD_STATE);
      setContinuationState(EMPTY_CONTINUATION_STATE);
      setCandidateForms({});
      setItemForms({});
      setContinuationForms({});
      return false;
    }
    const controller = new AbortController();
    const sequence = previous.sequence + 1;
    requestRef.current = { sequence, controller };
    setLoadState((current) => ({ ...current, status: "loading", error: "" }));
    try {
      const payload = await api.actionDesk(targetRoomId, controller.signal);
      if (
        controller.signal.aborted
        || requestRef.current.sequence !== sequence
        || activeRoomIdRef.current !== targetRoomId
      ) return false;
      const desk = normalizeActionDeskResponse(payload, targetRoomId);
      setLoadState({ status: desk.valid ? "ready" : "integrity_failed", desk, error: "" });
      setCandidateForms(formsFromRows(desk.candidates));
      setItemForms(formsFromRows(desk.items));
      return desk.valid;
    } catch (error) {
      if (controller.signal.aborted || error?.name === "AbortError") return false;
      if (requestRef.current.sequence !== sequence || activeRoomIdRef.current !== targetRoomId) return false;
      setLoadState({
        status: "error",
        desk: null,
        error: error?.message || "行动台暂时无法读取。",
      });
      return false;
    }
  }, [normalizedArtifactFingerprint, normalizedRoomId]);

  const loadContinuations = useCallback(async () => {
    const targetRoomId = normalizedRoomId;
    const previous = continuationRequestRef.current;
    previous.controller?.abort();
    if (!targetRoomId) {
      continuationRequestRef.current = { sequence: previous.sequence + 1, controller: null };
      setContinuationState(EMPTY_CONTINUATION_STATE);
      setContinuationForms({});
      return false;
    }
    const controller = new AbortController();
    const sequence = previous.sequence + 1;
    continuationRequestRef.current = { sequence, controller };
    setContinuationState((current) => ({ ...current, status: "loading", error: "" }));
    try {
      const payload = await api.actionDeskContinuations(targetRoomId, controller.signal);
      if (controller.signal.aborted || continuationRequestRef.current.sequence !== sequence || activeRoomIdRef.current !== targetRoomId) return false;
      const view = normalizeActionDeskContinuationsResponse(payload, targetRoomId);
      setContinuationState({ status: view.valid ? "ready" : "integrity_failed", view, error: "" });
      return view.valid;
    } catch (error) {
      if (controller.signal.aborted || error?.name === "AbortError") return false;
      if (continuationRequestRef.current.sequence !== sequence || activeRoomIdRef.current !== targetRoomId) return false;
      setContinuationState({ status: "error", view: null, error: error?.message || "行动延续记录暂时无法读取。" });
      return false;
    }
  }, [normalizedRoomId]);

  useEffect(() => {
    setMutation(EMPTY_MUTATION_STATE);
    void loadDesk();
    void loadContinuations();
    return () => {
      requestRef.current.controller?.abort();
      continuationRequestRef.current.controller?.abort();
      mutationControllerRef.current?.abort();
    };
  }, [loadContinuations, loadDesk]);

  const changeForm = (setForms, row, field, value) => {
    setForms((current) => ({
      ...current,
      [row.sourceKey]: {
        ...(current[row.sourceKey] || formFromRow(row)),
        [field]: value,
        clientRequestId: newActionDeskClientRequestId(),
      },
    }));
  };

  const submitTransition = async (row, transition, form) => {
    if (!row?.valid || !form || mutation.status === "loading") return;
    const targetRoomId = normalizedRoomId;
    let payload;
    try {
      payload = buildActionDeskTransitionRequest({
        source: row,
        transition,
        clientRequestId: form.clientRequestId,
        patch: {
          owner: form.owner,
          due: form.due,
          state: transition === "adopt" ? row.state : form.state,
          note: transition === "adopt" ? "" : form.note,
        },
      });
    } catch (error) {
      setMutation({ status: "error", sourceKey: row.sourceKey, error: error.message });
      return;
    }
    mutationControllerRef.current?.abort();
    const controller = new AbortController();
    mutationControllerRef.current = controller;
    setMutation({ status: "loading", sourceKey: row.sourceKey, error: "" });
    try {
      await api.transitionActionDesk(targetRoomId, payload, controller.signal);
      if (controller.signal.aborted || activeRoomIdRef.current !== targetRoomId) return;
      const [rereadReady] = await Promise.all([loadDesk(), loadContinuations()]);
      if (controller.signal.aborted || activeRoomIdRef.current !== targetRoomId) return;
      setMutation(rereadReady
        ? EMPTY_MUTATION_STATE
        : {
          status: "error",
          sourceKey: row.sourceKey,
          error: "变更已提交，但行动台重读失败；请重新读取后确认结果。",
        });
    } catch (error) {
      if (controller.signal.aborted || error?.name === "AbortError") return;
      if (activeRoomIdRef.current !== targetRoomId) return;
      setMutation({
        status: "error",
        sourceKey: row.sourceKey,
        error: error?.message || "行动项保存失败。",
      });
    }
  };

  const changeContinuationForm = (row, field, value) => {
    setContinuationForms((current) => ({
      ...current,
      [row.sourceKey]: {
        ...(current[row.sourceKey] || continuationFormFromRow(row)),
        [field]: value,
        clientRequestId: newActionContinuationClientRequestId(),
      },
    }));
  };

  const submitContinuation = async (row, candidates, form) => {
    if (!row?.valid || !form || mutation.status === "loading") return;
    const target = candidates.find((candidate) => candidate.sourceKey === form.targetKey);
    let payload;
    try {
      payload = buildActionContinuationRequest({
        source: row,
        target,
        sourceRevision: row.revision,
        reason: form.reason,
        clientRequestId: form.clientRequestId,
      });
    } catch (error) {
      setMutation({ status: "error", sourceKey: `continuation:${row.sourceKey}`, error: error.message });
      return;
    }
    mutationControllerRef.current?.abort();
    const controller = new AbortController();
    mutationControllerRef.current = controller;
    const mutationKey = `continuation:${row.sourceKey}`;
    setMutation({ status: "loading", sourceKey: mutationKey, error: "" });
    try {
      await api.continueActionDesk(normalizedRoomId, payload, controller.signal);
      if (controller.signal.aborted || activeRoomIdRef.current !== normalizedRoomId) return;
      const [deskReady, continuationReady] = await Promise.all([loadDesk(), loadContinuations()]);
      if (controller.signal.aborted || activeRoomIdRef.current !== normalizedRoomId) return;
      setMutation(deskReady && continuationReady
        ? EMPTY_MUTATION_STATE
        : { status: "error", sourceKey: mutationKey, error: "延续已提交，但行动台重读失败；请重新读取后确认结果。" });
    } catch (error) {
      if (controller.signal.aborted || error?.name === "AbortError") return;
      if (activeRoomIdRef.current !== normalizedRoomId) return;
      setMutation({ status: "error", sourceKey: mutationKey, error: error?.message || "行动延续保存失败。" });
    }
  };

  const desk = loadState.desk;
  const counts = desk?.countsVisible ? desk.counts : null;
  const continuationView = continuationState.view;
  return (
    <section
      className="inspector-section action-desk-panel"
      id="inspector-action-desk"
      aria-label="独立行动台"
      aria-busy={loadState.status === "loading" || continuationState.status === "loading" || mutation.status === "loading"}
      tabIndex={-1}
    >
      <header className="action-desk-heading">
        <span><ClipboardList aria-hidden="true" size={16} /><strong>独立行动台</strong></span>
        <em>确认产物待办</em>
      </header>
      <p className="action-desk-intro">
        先从精确确认版本采纳，再由你维护负责人、期限和状态；不会把候选待办自动当成已执行任务。
      </p>

      {loadState.status === "loading" ? (
        <p className="action-desk-state" role="status" aria-live="polite"><LoaderCircle aria-hidden="true" className="spin" size={15} />正在读取精确行动来源……</p>
      ) : null}
      {loadState.status === "error" ? (
        <p className="action-desk-state error" role="alert">
          <AlertTriangle aria-hidden="true" size={15} />
          <span><strong>行动台读取失败</strong><small>{loadState.error}</small></span>
          <button className="secondary compact" type="button" onClick={() => void loadDesk()}>重试</button>
        </p>
      ) : null}
      {loadState.status === "integrity_failed" ? (
        <p className="action-desk-state error" role="alert">
          <AlertTriangle aria-hidden="true" size={15} />
          <span>
            <strong>行动台完整性或安全边界校验失败</strong>
            <small>全部候选、行动项和计数已隐藏；不会用当前产物替代。</small>
          </span>
        </p>
      ) : null}
      {continuationState.status === "error" ? (
        <p className="action-desk-state warning" role="note">
          <AlertTriangle aria-hidden="true" size={15} />
          <span><strong>新版延续记录暂不可用</strong><small>{continuationState.error}</small></span>
          <button className="secondary compact" type="button" onClick={() => void loadContinuations()}>重试</button>
        </p>
      ) : null}
      {continuationState.status === "integrity_failed" ? (
        <p className="action-desk-state warning" role="note">
          <AlertTriangle aria-hidden="true" size={15} />
          <span><strong>延续谱系完整性无法确认</strong><small>已隐藏新版关系；旧行动状态保持原样。</small></span>
        </p>
      ) : null}

      {loadState.status === "ready" && desk?.valid ? (
        <>
          {counts ? (
            <div className="action-desk-counts" aria-label="行动台计数">
              <span><small>待采纳</small><strong>{counts.candidateCount}</strong></span>
              <span><small>待处理</small><strong>{counts.openCount}</strong></span>
              <span><small>进行中</small><strong>{counts.inProgressCount}</strong></span>
              <span><small>受阻</small><strong>{counts.blockedCount}</strong></span>
              <span><small>已完成</small><strong>{counts.doneCount}</strong></span>
            </div>
          ) : (
            <p className="action-desk-state warning" role="note">
              <AlertTriangle aria-hidden="true" size={15} />条目状态或来源损坏，汇总计数已隐藏。
            </p>
          )}

          <div className="action-desk-group">
            <div className="action-desk-group-heading"><strong>待采纳</strong><span>{desk.candidates.length} 项</span></div>
            {desk.candidates.length ? desk.candidates.map((row) => (
              row.valid ? (
                <CandidateCard
                  key={`candidate:${row.index}:${row.sourceKey}`}
                  row={row}
                  form={candidateForms[row.sourceKey] || formFromRow(row)}
                  locked={mutation.status === "loading"}
                  saving={mutation.status === "loading" && mutation.sourceKey === row.sourceKey}
                  onChange={(field, value) => changeForm(setCandidateForms, row, field, value)}
                  onAdopt={() => void submitTransition(
                    row,
                    "adopt",
                    candidateForms[row.sourceKey] || formFromRow(row),
                  )}
                />
              ) : <RedactedRow key={`candidate:${row.index}`} kind="candidate" />
            )) : <p className="action-desk-empty">当前没有等待采纳的确认产物待办。</p>}
          </div>

          <div className="action-desk-group">
            <div className="action-desk-group-heading"><strong>已采纳行动项</strong><span>{desk.items.length} 项</span></div>
            {desk.items.length ? desk.items.map((row) => (
              row.valid ? (
                <ItemCard
                  key={`item:${row.index}:${row.sourceKey}`}
                  row={row}
                  form={itemForms[row.sourceKey] || formFromRow(row)}
                  locked={mutation.status === "loading"}
                  saving={mutation.status === "loading" && mutation.sourceKey === row.sourceKey}
                  onChange={(field, value) => changeForm(setItemForms, row, field, value)}
                  onSave={() => void submitTransition(
                    row,
                    "update",
                    itemForms[row.sourceKey] || formFromRow(row),
                  )}
                  onFillComposer={() => onFillComposer?.({
                    roomId: normalizedRoomId,
                    text: actionDeskComposerText(row),
                    source: {
                      artifactId: row.artifactId,
                      artifactVersion: row.artifactVersion,
                      actionId: row.actionId,
                      actionSnapshotSha256: row.actionSnapshotSha256,
                    },
                  })}
                  continuation={(!row.sourceCurrent && continuationState.status === "ready" && continuationView?.metricsVisible) ? (() => {
                    const candidates = continuationTargetCandidates(row, desk.candidates);
                    return {
                      relation: continuationView.relationBySource.get(row.sourceKey) || null,
                      candidates,
                      form: continuationForms[row.sourceKey] || continuationFormFromRow(row, candidates),
                      saving: mutation.status === "loading" && mutation.sourceKey === `continuation:${row.sourceKey}`,
                      onChange: (field, value) => changeContinuationForm(row, field, value),
                      onSubmit: () => void submitContinuation(
                        row,
                        candidates,
                        continuationForms[row.sourceKey] || continuationFormFromRow(row, candidates),
                      ),
                    };
                  })() : null}
                />
              ) : <RedactedRow key={`item:${row.index}`} kind="item" />
            )) : <p className="action-desk-empty">尚未采纳行动项。</p>}
          </div>

          {mutation.status === "error" ? (
            <p className="action-desk-state error" role="alert">
              <AlertTriangle aria-hidden="true" size={15} />{mutation.error}
            </p>
          ) : null}
        </>
      ) : null}

      <p className="action-desk-boundary">
        <ShieldCheck aria-hidden="true" size={14} />只管理本地共创待办，不执行外部写入、不自动开始讨论、不替代用户最终决定。
      </p>
    </section>
  );
});
