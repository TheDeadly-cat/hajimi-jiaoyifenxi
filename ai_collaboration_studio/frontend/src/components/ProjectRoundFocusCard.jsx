import {
  AlertTriangle,
  CheckCircle2,
  ClipboardPenLine,
  LoaderCircle,
  ShieldCheck,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import {
  buildProjectRoundFocusAuthorization,
  normalizeProjectRoundFocusResponse,
  projectRoundFocusArtifactFingerprint,
  projectRoundFocusAuthorizationState,
  projectRoundFocusCardSource,
  projectRoundFocusLoadPlan,
  projectRoundFocusRoomContextFingerprint,
} from "../projectRoundFocus";

function initialState() {
  return { status: "idle", preview: null, record: null, error: "" };
}

function categoryLabel(category) {
  if (category === "blocker") return "阻断";
  if (category === "evidence") return "证据";
  return "结构";
}

function FocusItem({ item }) {
  return (
    <li className={`project-round-focus-item ${item.category}`}>
      <span className="project-round-focus-sequence">{item.sequenceNo}</span>
      <span>
        <strong>{item.message}</strong>
        <small>{categoryLabel(item.category)} · {item.itemKey}</small>
        {item.targetCapabilities.length ? (
          <span className="project-round-focus-capabilities" aria-label="建议目标能力">
            {item.targetCapabilities.map((capability) => <em key={capability}>{capability}</em>)}
          </span>
        ) : null}
      </span>
    </li>
  );
}

export function ProjectRoundFocusCard({
  room,
  members = [],
  artifacts = [],
  pendingRound = null,
  slot,
  contribution,
  authorization = null,
  onFillObjective,
}) {
  const artifactFingerprint = useMemo(
    () => projectRoundFocusArtifactFingerprint(artifacts),
    [artifacts],
  );
  const roomContextFingerprint = useMemo(
    () => projectRoundFocusRoomContextFingerprint({ room, members }),
    [members, room],
  );
  const plan = useMemo(() => projectRoundFocusLoadPlan({
    room,
    pendingRound,
    slot,
    contribution,
    showLegacyFallback: Boolean(contribution?.present),
    artifactFingerprint,
    roomContextFingerprint,
  }), [artifactFingerprint, contribution, pendingRound, room, roomContextFingerprint, slot]);
  const [state, setState] = useState(initialState);

  useEffect(() => {
    if (!plan.shouldLoad || !plan.expected) {
      setState(initialState);
      return undefined;
    }
    let cancelled = false;
    const controller = new AbortController();
    setState({ status: "loading", preview: null, record: null, error: "" });
    const request = plan.requestKind === "record"
      ? api.projectRoundFocusRecord(
        plan.expected.roomId,
        plan.expected.roundId,
        controller.signal,
      )
      : api.projectRoundFocus(plan.expected.roomId, controller.signal);
    request.then((payload) => {
      if (cancelled) return;
      const view = normalizeProjectRoundFocusResponse(payload, plan.expected);
      setState({
        status: view.valid ? "ready" : "integrity_failed",
        preview: view.kind === "preview" ? view : null,
        record: view.kind === "record" ? view : null,
        error: "",
      });
    }).catch((error) => {
      if (cancelled || error?.name === "AbortError") return;
      setState({
        status: "error",
        preview: null,
        record: null,
        error: error?.message || "下一轮焦点暂时无法读取。",
      });
    });
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [plan.requestKey]);

  if (!plan.visible) return null;
  const view = projectRoundFocusCardSource(state);
  const shownItems = view?.focusItems.slice(0, 3) || [];
  const remainingItems = view?.focusItems.slice(3) || [];
  const authorizationState = projectRoundFocusAuthorizationState(authorization, {
    roomId: room?.id,
    artifactFingerprint,
    roomContextFingerprint,
    pluginRegistrySnapshotSha256: view?.pluginRegistrySnapshotSha256,
  });
  const canFill = Boolean(
    view?.valid
    && view.kind === "preview"
    && typeof onFillObjective === "function"
    && !pendingRound,
  );
  const exactArtifact = view?.artifactBinding?.status === "exact";

  const fillObjective = () => {
    if (!canFill) return;
    onFillObjective({
      roomId: view.roomId,
      objective: view.suggestedObjective,
      artifactFingerprint,
      roomContextFingerprint,
      pluginRegistrySnapshotSha256: view.pluginRegistrySnapshotSha256,
      request: buildProjectRoundFocusAuthorization(view),
    });
  };

  return (
    <section
      className={`inspector-section project-round-focus-card ${plan.shouldLoad ? "available" : "read-only"}`}
      aria-label="下一轮项目焦点"
    >
      <header className="project-round-focus-heading">
        <span>
          <ClipboardPenLine size={16} />
          <strong>{view?.kind === "record" ? "本轮冻结项目焦点" : "下一轮项目焦点"}</strong>
        </span>
        <em>{view?.kind === "record" ? "已冻结" : "只读预览"}</em>
      </header>

      {!plan.shouldLoad ? (
        <p className="project-round-focus-state warning" role="note">
          <AlertTriangle size={15} />
          <span><strong>当前仅保留只读说明</strong><small>{plan.reason}</small></span>
        </p>
      ) : null}
      {plan.shouldLoad && state.status === "loading" ? (
        <p className="project-round-focus-state" aria-live="polite">
          <LoaderCircle className="spin" size={15} />正在读取精确焦点封印……
        </p>
      ) : null}
      {plan.shouldLoad && state.status === "error" ? (
        <p className="project-round-focus-state error" role="alert">
          <AlertTriangle size={15} />
          <span><strong>焦点读取失败，未填入目标</strong><small>{state.error}</small></span>
        </p>
      ) : null}
      {plan.shouldLoad && state.status === "integrity_failed" ? (
        <p className="project-round-focus-state error" role="alert">
          <AlertTriangle size={15} />
          <span><strong>焦点绑定或安全字段校验失败</strong><small>全部焦点、计数和建议目标已隐藏。</small></span>
        </p>
      ) : null}

      {view?.valid ? (
        <>
          <div className={`project-round-focus-source ${exactArtifact ? "exact" : "bootstrap"}`}>
            {exactArtifact ? <CheckCircle2 size={15} /> : <ShieldCheck size={15} />}
            <span>
              <strong>
                {exactArtifact
                  ? `${view.artifactBinding.artifactTitle} · v${view.artifactBinding.artifactVersion}`
                  : "暂无确认产物 · bootstrap 上下文"}
              </strong>
              <small>
                {exactArtifact
                  ? "焦点只来自这个精确确认版本，不会静默切换到更新产物。"
                  : "未生成任何产物缺口清单；仅可显式沿用当前房间目标。"}
              </small>
            </span>
          </div>

          <div className="project-round-focus-counts" aria-label="下一轮焦点计数">
            <span><small>结构缺口</small><strong>{view.counts.structuralGapCount}</strong></span>
            <span><small>阻断条件</small><strong>{view.counts.blockerCount}</strong></span>
            <span><small>证据缺口</small><strong>{view.counts.evidenceGapCount}</strong></span>
          </div>

          {shownItems.length ? (
            <ol className="project-round-focus-list">
              {shownItems.map((item) => <FocusItem key={`${item.sequenceNo}:${item.code}:${item.itemKey}`} item={item} />)}
            </ol>
          ) : (
            <p className="project-round-focus-empty">
              {exactArtifact ? "当前精确产物未产生合同定义的下一轮焦点。" : "暂无确认产物，未生成缺口清单。"}
            </p>
          )}
          {remainingItems.length ? (
            <p className="project-round-focus-more">
              另有 {remainingItems.length} 条焦点，请打开精确确认产物查看完整缺口。
            </p>
          ) : null}

          <div className="project-round-focus-objective">
            <small>建议的下一轮目标</small>
            <p>{view.suggestedObjective}</p>
            {view.kind === "preview" ? (
              <button
                className="secondary compact"
                type="button"
                onClick={fillObjective}
                disabled={!canFill || authorizationState.valid}
              >
                <ClipboardPenLine size={14} />
                {authorizationState.valid ? "已填入下一轮目标" : "填入下一轮目标"}
              </button>
            ) : (
              <small className="project-round-focus-frozen-note">
                这是当前轮次的冻结记录，不能回填到下一轮。
              </small>
            )}
          </div>
        </>
      ) : null}

      <p className="project-round-focus-boundary">
        只填入可编辑目标，不自动开始、不点名成员、不改变流程或用户最终决定。
      </p>
    </section>
  );
}
