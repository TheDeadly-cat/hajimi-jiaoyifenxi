import {
  AlertTriangle,
  CheckCircle2,
  ClipboardPenLine,
  LoaderCircle,
  ShieldCheck,
} from "lucide-react";
import { memo, useEffect, useId, useMemo, useState } from "react";
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
import "../styles/project-round-focus-polish.css";

const EMPTY_LIST = Object.freeze([]);

function initialState() {
  return { status: "idle", preview: null, record: null, error: "" };
}

function categoryLabel(category) {
  if (category === "blocker") return "阻断";
  if (category === "evidence") return "证据";
  return "结构";
}

const FocusItem = memo(function FocusItem({ item }) {
  return (
    <li className={`project-round-focus-item ${item.category}`}>
      <span className="project-round-focus-sequence">{item.sequenceNo}</span>
      <span>
        <strong>{item.message}</strong>
        <small>{categoryLabel(item.category)} · {item.itemKey}</small>
        {item.targetCapabilities.length ? (
          <span className="project-round-focus-capabilities" aria-label="建议目标能力">
            {item.targetCapabilities.map((capability, capabilityIndex) => (
              <em key={`${capability}:${capabilityIndex}`}>{capability}</em>
            ))}
          </span>
        ) : null}
      </span>
    </li>
  );
});

export const ProjectRoundFocusCard = memo(function ProjectRoundFocusCard({
  room,
  members = EMPTY_LIST,
  artifacts = EMPTY_LIST,
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
  const [requestRevision, setRequestRevision] = useState(0);
  const [objectiveExpanded, setObjectiveExpanded] = useState(false);
  const headingId = useId();
  const objectiveId = useId();
  const focusListHeadingId = useId();
  const boundaryId = useId();

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
  }, [plan.requestKey, requestRevision]);

  useEffect(() => {
    setObjectiveExpanded(false);
  }, [plan.requestKey]);

  if (!plan.visible) return null;
  const view = projectRoundFocusCardSource(state);
  const shownItems = view?.focusItems.slice(0, 3) || [];
  const remainingItems = view?.focusItems.slice(3) || [];
  const totalFocusCount = view?.focusItems.length || 0;
  const primaryItem = shownItems[0] || null;
  const objectiveCanCollapse = (view?.suggestedObjective?.length || 0) > 160;
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
      id="inspector-project-focus"
      tabIndex={-1}
      className={`inspector-section project-round-focus-card ${plan.shouldLoad ? "available" : "read-only"}`}
      aria-labelledby={headingId}
      aria-busy={plan.shouldLoad && state.status === "loading"}
    >
      <header className="project-round-focus-heading">
        <span>
          <ClipboardPenLine aria-hidden="true" size={16} />
          <h3 id={headingId}>{view?.kind === "record" ? "本轮冻结项目焦点" : "下一轮项目焦点"}</h3>
        </span>
        <span className="project-round-focus-mode">{view?.kind === "record" ? "已冻结" : "只读预览"}</span>
      </header>

      {!plan.shouldLoad ? (
        <p className="project-round-focus-state warning" role="note">
          <AlertTriangle aria-hidden="true" size={15} />
          <span><strong>当前仅保留只读说明</strong><small>{plan.reason}</small></span>
        </p>
      ) : null}
      {plan.shouldLoad && state.status === "loading" ? (
        <p className="project-round-focus-state" role="status" aria-live="polite">
          <LoaderCircle aria-hidden="true" className="spin" size={15} />正在读取精确焦点封印……
        </p>
      ) : null}
      {plan.shouldLoad && state.status === "error" ? (
        <p className="project-round-focus-state error" role="alert">
          <AlertTriangle aria-hidden="true" size={15} />
          <span><strong>焦点读取失败，未填入目标</strong><small>{state.error}</small></span>
          <button className="secondary compact" type="button" onClick={() => setRequestRevision((current) => current + 1)}>重试</button>
        </p>
      ) : null}
      {plan.shouldLoad && state.status === "integrity_failed" ? (
        <p className="project-round-focus-state error" role="alert">
          <AlertTriangle aria-hidden="true" size={15} />
          <span><strong>焦点绑定或安全字段校验失败</strong><small>全部焦点、计数和建议目标已隐藏。</small></span>
          <button className="secondary compact" type="button" onClick={() => setRequestRevision((current) => current + 1)}>重新读取</button>
        </p>
      ) : null}

      {view?.valid ? (
        <>
          <div className={`project-round-focus-source ${exactArtifact ? "exact" : "bootstrap"}`} role="note">
            {exactArtifact ? <CheckCircle2 aria-hidden="true" size={15} /> : <ShieldCheck aria-hidden="true" size={15} />}
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

          <div className="project-round-focus-counts" role="list" aria-label="下一轮焦点计数">
            <span className="structural" role="listitem"><small>结构缺口</small><strong>{view.counts.structuralGapCount}</strong></span>
            <span className="blocker" role="listitem"><small>阻断条件</small><strong>{view.counts.blockerCount}</strong></span>
            <span className="evidence" role="listitem"><small>证据缺口</small><strong>{view.counts.evidenceGapCount}</strong></span>
          </div>

          <div className="project-round-focus-objective" data-priority={primaryItem?.category || "continuation"}>
            <header>
              <span>下一步 <small>NEXT PASS</small></span>
              <em>{primaryItem ? `${categoryLabel(primaryItem.category)}优先` : "目标续接"}</em>
            </header>
            <p
              className={objectiveCanCollapse && !objectiveExpanded ? "is-collapsed" : undefined}
              id={objectiveId}
            >
              {view.suggestedObjective}
            </p>
            {objectiveCanCollapse || view.kind === "preview" ? (
              <div className="project-round-focus-objective-actions">
                {objectiveCanCollapse ? (
                  <button
                    className="project-round-focus-objective-toggle"
                    type="button"
                    aria-controls={objectiveId}
                    aria-expanded={objectiveExpanded}
                    onClick={() => setObjectiveExpanded((current) => !current)}
                  >
                    {objectiveExpanded ? "收起完整目标" : "展开完整目标"}
                  </button>
                ) : null}
                {view.kind === "preview" ? (
                  <button
                    className="secondary compact"
                    type="button"
                    onClick={fillObjective}
                    disabled={!canFill || authorizationState.valid}
                    aria-describedby={boundaryId}
                  >
                    <ClipboardPenLine aria-hidden="true" size={14} />
                    {authorizationState.valid ? "已填入下一轮目标" : "填入下一轮目标"}
                  </button>
                ) : null}
              </div>
            ) : null}
            {view.kind !== "preview" ? (
              <small className="project-round-focus-frozen-note">
                这是当前轮次的冻结记录，不能回填到下一轮。
              </small>
            ) : null}
          </div>

          {shownItems.length ? (
            <>
              <div className="project-round-focus-list-heading">
                <span>
                  <strong id={focusListHeadingId}>本轮优先焦点</strong>
                  <small>按封印顺序，最多显示前三条</small>
                </span>
                <data
                  value={`${shownItems.length}/${totalFocusCount}`}
                  aria-label={`已显示 ${shownItems.length} / ${totalFocusCount} 条焦点`}
                >
                  {shownItems.length} / {totalFocusCount}
                </data>
              </div>
              <ol className="project-round-focus-list" aria-labelledby={focusListHeadingId}>
                {shownItems.map((item) => <FocusItem key={`${item.sequenceNo}:${item.code}:${item.itemKey}`} item={item} />)}
              </ol>
            </>
          ) : (
            <p className="project-round-focus-empty">
              {exactArtifact ? "当前精确产物未产生合同定义的下一轮焦点。" : "暂无确认产物，未生成缺口清单。"}
            </p>
          )}
          {remainingItems.length ? (
            <p className="project-round-focus-more">
              另有 {remainingItems.length} 条焦点。完整缺口保留在精确确认产物中；当前仅展示前三条，避免本面板替代产物审阅。
            </p>
          ) : null}
        </>
      ) : null}

      <div id={boundaryId} className="project-round-focus-boundary" role="list" aria-label="下一轮目标操作边界">
        <span role="listitem"><strong>只预填</strong><small>目标仍可编辑</small></span>
        <span role="listitem"><strong>不自动开始</strong><small>不点名或改流程</small></span>
        <span role="listitem"><strong>用户最终决定</strong><small>不生成批准结论</small></span>
      </div>
    </section>
  );
});
