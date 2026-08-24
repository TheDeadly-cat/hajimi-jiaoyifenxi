import { ArrowDown, ArrowUp, CheckSquare, Database, FilePlus2, FlaskConical, GitBranch, History, Link2, Network, Pause, Play, RotateCcw, ShieldCheck, SlidersHorizontal, UserPlus, Users, X } from "lucide-react";
import { lazy, memo, Suspense, useEffect, useMemo, useRef, useState } from "react";
import { directorSourceLabel } from "../directorDecision";
import { ArtifactPanel } from "./ArtifactPanel";
import { ActionDeskPanel } from "./ActionDeskPanel";
import { ConvergenceCard } from "./ConvergenceCard";
import { ProviderRoutingPanel } from "./ProviderRoutingPanel";
import { DirectorModeratorAttribution } from "./DirectorModeratorAttribution";
import { RoundExecutionTraceSummary } from "./RoundExecutionTraceSummary";
import { buildProviderRouteSummary } from "../providerRouting";
import { ProjectRoundFocusCard } from "./ProjectRoundFocusCard";
import { materialPromptQuarantine, materialSourceLabel } from "../materials";
import {
  HOST_CONTRIBUTION_IDS,
  HOST_SLOT_IDS,
  resolveHostOwnedSlot,
  resolvedHostContribution,
} from "../capabilityContributions";
import { roomDomainCapabilityLabels } from "../roomCapabilities";
import { deriveRoundAvailability } from "../roundAvailability";
import { meetingReadinessAnnouncementText } from "../liveRegionAnnouncements";
import { policiesEqual, stageLabel } from "../workflowPolicy";
import { bindInspectorTargetNavigation } from "../inspectorTargetNavigation";
import {
  ROOM_INSPECTOR_ARCHIVED_MEMBER_LIMIT,
  ROOM_INSPECTOR_ARCHIVED_MEMBER_STEP,
  ROOM_INSPECTOR_INITIAL_ARCHIVED_MEMBER_LIMIT,
  ROOM_INSPECTOR_INITIAL_MATERIAL_LIMIT,
  ROOM_INSPECTOR_INITIAL_MEMBER_LIMIT,
  ROOM_INSPECTOR_MATERIAL_LIMIT,
  ROOM_INSPECTOR_MATERIAL_STEP,
  ROOM_INSPECTOR_MEMBER_LIMIT,
  ROOM_INSPECTOR_MEMBER_STEP,
  buildCurrentRoundDirectorDecisions,
  buildRoomInspectorArtifactFingerprint,
  buildRoomInspectorListProjection,
  buildRoomInspectorProviderIndex,
  buildRoomInspectorWorkflowProjection,
  inspectWalkForwardFootprint,
  roomInspectorErrorMessage,
  safeRoomInspectorColor,
  safeRoomInspectorText,
} from "../roomInspectorView";
import "../styles/room-inspector-refinement.css";

const ObservationPanel = lazy(() => import("./ObservationPanel.jsx")
  .then((module) => ({ default: module.ObservationPanel })));
const PaperPortfolioPanel = lazy(() => import("./PaperPortfolioPanel.jsx")
  .then((module) => ({ default: module.PaperPortfolioPanel })));
const DecisionLineagePanel = lazy(() => import("./DecisionLineagePanel.jsx")
  .then((module) => ({ default: module.DecisionLineagePanel })));
const MarketSnapshotCard = lazy(() => import("./MarketSnapshotCard.jsx")
  .then((module) => ({ default: module.MarketSnapshotCard })));
const StorageSampleAcceptanceCard = lazy(() => import("./StorageSampleAcceptanceCard.jsx")
  .then((module) => ({ default: module.StorageSampleAcceptanceCard })));

function InspectorPanelFallback({ label }) {
  return (
    <section className="inspector-section inspector-panel-loading" aria-busy="true" aria-label={label}>
      <span className="inspector-panel-loading-mark" aria-hidden="true" />
      <span><strong>{label}</strong><small>正在载入只读研究记录…</small></span>
    </section>
  );
}

function statusText(status) {
  if (status === "speaking") return "发言中";
  if (status === "done") return "已完成";
  if (status === "failed") return "未完成";
  if (status === "skipped") return "已跳过";
  return "等待";
}

function PluginActionBoundary({ disabled, label, children }) {
  return (
    <fieldset className={disabled ? "plugin-action-boundary read-only" : "plugin-action-boundary"} disabled={disabled}>
      <legend>{label}</legend>
      {children}
    </fieldset>
  );
}

export const RoomInspector = memo(function RoomInspector({
  room,
  pluginRegistry,
  pluginLifecycle,
  members = [],
  archivedMembers = [],
  providers = [],
  templateWorkflowPolicy,
  roundState = {},
  directorDecisions = [],
  latestRound,
  pendingRound,
  pendingRoundCheckpoint,
  convergence,
  storageSampleAcceptance,
  workflowConfiguration,
  marketSnapshot,
  marketStatus,
  marketReadiness,
  marketLoading,
  marketReadinessLoading,
  marketGate,
  materials = [],
  artifacts = [],
  artifactLoading,
  decisionPackages = [],
  observations = [],
  reflections = [],
  paperPortfolios = [],
  walkForwardRunsByPortfolio = {},
  walkForwardLoadingByPortfolio = {},
  walkForwardErrorsByPortfolio = {},
  candidateComparison,
  candidateComparisonLoading,
  candidateComparisonError,
  observationScorecard,
  observationLoading,
  paperPortfolioLoading,
  onEditMember,
  onAddMember,
  onMoveMember,
  onViewMemberHistory,
  onRestoreMember,
  onEditWorkflowPolicy,
  onStartRound,
  onPause,
  onResumeRound,
  onEndRound,
  onRefreshMarket,
  onRefreshMarketReadiness,
  onFreezeOfficialEvidence,
  onAddOfficialSupplement,
  onAddMaterial,
  onEditMaterial,
  onGenerateArtifact,
  onEditArtifact,
  onFillActionDeskComposer,
  onAddObservation,
  onAddObservationFromDecision,
  onBindObservationDecisionLineage,
  onConfirmObservation,
  onReconcileObservations,
  onEditReflection,
  onAddPaperPortfolio,
  onAddPaperPortfolioFromDecision,
  onEditPaperPortfolio,
  onConfirmPaperPortfolio,
  onEvaluatePaperPortfolio,
  onRunPaperPortfolioWalkForward,
  onCompareCandidates,
  onRouteMembers,
  onRunProviderPreflight,
  roundFocusAuthorization = null,
  onFillRoundFocusObjective,
  roundFocusRequired = false,
  roundFocusReady = true,
  routingBusy,
  roundProviderReady,
  roundProviderBlockReason,
  providerPreflightState,
  roundExecutionTraceState = null,
  onOpenRoundExecutionTrace,
  endingRound = false,
  scrollTargetId = "",
  scrollRequestId = 0,
}) {
  const inspectorRef = useRef(null);
  const roundActionRef = useRef(false);
  const [memberLimit, setMemberLimit] = useState(ROOM_INSPECTOR_INITIAL_MEMBER_LIMIT);
  const [archivedMemberLimit, setArchivedMemberLimit] = useState(
    ROOM_INSPECTOR_INITIAL_ARCHIVED_MEMBER_LIMIT,
  );
  const [materialLimit, setMaterialLimit] = useState(ROOM_INSPECTOR_INITIAL_MATERIAL_LIMIT);
  const [pendingRoundAction, setPendingRoundAction] = useState("");
  const [roundActionError, setRoundActionError] = useState("");
  useEffect(() => {
    if (!scrollTargetId || !scrollRequestId) return undefined;
    return bindInspectorTargetNavigation(inspectorRef.current, scrollTargetId);
  }, [scrollRequestId, scrollTargetId]);
  useEffect(() => {
    setMemberLimit(ROOM_INSPECTOR_INITIAL_MEMBER_LIMIT);
    setArchivedMemberLimit(ROOM_INSPECTOR_INITIAL_ARCHIVED_MEMBER_LIMIT);
    setMaterialLimit(ROOM_INSPECTOR_INITIAL_MATERIAL_LIMIT);
    setRoundActionError("");
  }, [room?.id]);
  const listProjection = useMemo(
    () => buildRoomInspectorListProjection({
      members,
      archivedMembers,
      materials,
      memberLimit,
      archivedMemberLimit,
      materialLimit,
    }),
    [
      archivedMemberLimit,
      archivedMembers,
      materialLimit,
      materials,
      memberLimit,
      members,
    ],
  );
  const providerIndex = useMemo(
    () => buildRoomInspectorProviderIndex(providers),
    [providers],
  );
  const boundedMembers = listProjection.members.boundedRows;
  const providerReady = roundProviderReady
    ?? providerIndex.rows.some((provider) => provider.configured === true);
  const roundBusy = roundState.running || roundState.pausing;
  const roundControlBusy = roundBusy || Boolean(pendingRoundAction);
  const roundAvailability = useMemo(
    () => deriveRoundAvailability({
      pending_round: pendingRound,
      pending_round_checkpoint: pendingRoundCheckpoint,
    }),
    [pendingRound, pendingRoundCheckpoint],
  );
  const memberLifecycleLocked = roundControlBusy || roundAvailability.hasPendingRound;
  const pausedRoundPending = roundAvailability.pausedRoundPending;
  const canResume = roundAvailability.canResume;
  const workflowReady = workflowConfiguration?.ready ?? true;
  const marketReady = marketGate?.ready ?? true;
  const canStart = !roundAvailability.hasPendingRound
    && workflowReady
    && providerReady
    && marketReady
    && (!roundFocusRequired || roundFocusReady);
  const workflowBlockReason = workflowReady
    ? ""
    : `讨论配置不可执行：${workflowConfiguration?.blockers?.[0]?.title || "存在成员缺口"}。${workflowConfiguration?.blockers?.[0]?.detail || ""}`;
  const startBlockReason = roundAvailability.blockReason
    || workflowBlockReason
    || roundProviderBlockReason
    || marketGate?.reason
    || (roundFocusRequired && !roundFocusReady
      ? "请先显式填入下一轮项目焦点。"
      : "")
    || "";
  const frozenMarket = pendingRoundCheckpoint?.frozen_market;
  const providerCheckStatus = !providerReady ? "blocked" : providerPreflightState?.status || "idle";
  const currentRoundId = roundState.roundId || pendingRound?.id || latestRound?.id || "";
  const currentRoundStatus = roundState.running
    ? roundState.pausing ? "正在暂停" : "进行中"
    : String(pendingRound?.status || latestRound?.status || "").toUpperCase() === "PAUSED"
      ? "已暂停"
      : String(latestRound?.status || "").toUpperCase() === "COMPLETED"
        ? "已完成"
        : String(latestRound?.status || "").toUpperCase() === "CANCELLED"
          ? "已结束"
          : "";
  const visibleTraceState = roundExecutionTraceState?.roundId === currentRoundId
    ? roundExecutionTraceState
    : null;
  const directorDecisionProjection = useMemo(
    () => buildCurrentRoundDirectorDecisions(directorDecisions, currentRoundId),
    [currentRoundId, directorDecisions],
  );
  const currentRoundDirectorDecisions = directorDecisionProjection.rows;
  const persistedDirectorDecision = currentRoundDirectorDecisions.at(-1);
  const directorDecision = roundState.directorDecision || (persistedDirectorDecision ? {
    action: persistedDirectorDecision.action,
    member: persistedDirectorDecision.member_id
      ? { id: persistedDirectorDecision.member_id, name: persistedDirectorDecision.member_name }
      : null,
    reason: persistedDirectorDecision.reason,
    source: persistedDirectorDecision.source,
    stage: persistedDirectorDecision.stage,
    workspaceFocus: persistedDirectorDecision.workspace_focus,
    moderatorContext: persistedDirectorDecision.moderator_context || null,
  } : null);
  const workflowProjection = useMemo(
    () => buildRoomInspectorWorkflowProjection(room?.workflow_policy),
    [room?.workflow_policy],
  );
  const workflowPolicy = workflowProjection.workflowPolicy;
  const templateDefault = policiesEqual(workflowPolicy, templateWorkflowPolicy);
  const requiredCoverage = workflowProjection.requiredCoverage;
  const routeSummary = useMemo(
    () => buildProviderRouteSummary(boundedMembers, providerIndex.rows),
    [boundedMembers, providerIndex.rows],
  );
  const roomInspectorRegistrySource = pendingRound || room;
  const roomInspectorSlot = useMemo(
    () => resolveHostOwnedSlot({
      slotId: HOST_SLOT_IDS.roomInspector,
      frozenContext: roomInspectorRegistrySource,
      pluginRegistry,
      pluginLifecycle,
    }),
    [pluginLifecycle, pluginRegistry, roomInspectorRegistrySource],
  );
  const storageContribution = resolvedHostContribution(
    roomInspectorSlot,
    HOST_CONTRIBUTION_IDS.storageRoomInspector,
  );
  const projectRoundFocusContribution = resolvedHostContribution(
    roomInspectorSlot,
    HOST_CONTRIBUTION_IDS.projectRoundFocusRoomInspector,
  );
  const stockResearchContribution = resolvedHostContribution(
    roomInspectorSlot,
    HOST_CONTRIBUTION_IDS.stockResearchRoomInspector,
  );
  const stockRoomScopeSymbols = Array.isArray(room?.stock_room_scope?.symbols)
    ? room.stock_room_scope.symbols
    : [];
  const walkForwardFootprint = useMemo(
    () => inspectWalkForwardFootprint(walkForwardRunsByPortfolio),
    [walkForwardRunsByPortfolio],
  );
  const walkForwardHistoryExists = walkForwardFootprint.hasHistory;
  const storageHistoricalFootprint = Boolean(
    storageSampleAcceptance
    || decisionPackages.length
    || (paperPortfolios || []).length
    || (observations || []).length
    || (reflections || []).length
    || walkForwardHistoryExists
    || candidateComparison,
  );
  const storageWorkspaceVisible = Boolean(storageContribution?.present || storageHistoricalFootprint);
  const storageReadOnly = storageWorkspaceVisible && storageContribution?.active !== true;
  const storageReadOnlyReason = roomInspectorSlot.reason
    || "历史存储研究记录仍保留，但当前冻结合同不可执行新操作。";
  const paperPortfolioVisible = Boolean(
    storageContribution?.present
    || (paperPortfolios || []).length
    || walkForwardHistoryExists
    || candidateComparison,
  );
  const observationsVisible = Boolean(
    storageContribution?.present
    || (observations || []).length
    || (reflections || []).length,
  );
  const decisionLineageVisible = Boolean(
    storageContribution?.present
    || decisionPackages.length
    || (paperPortfolios || []).length
    || (observations || []).length
    || walkForwardHistoryExists,
  );
  const marketWorkspaceVisible = Boolean(
    storageContribution?.present
    || marketSnapshot
    || marketStatus
    || marketReadiness,
  );
  const rawDomainCapabilityLabels = useMemo(
    () => roomDomainCapabilityLabels(room),
    [room],
  );
  const domainCapabilityLabels = Array.isArray(rawDomainCapabilityLabels)
    ? rawDomainCapabilityLabels.slice(0, 20)
    : [];
  const domainCapabilityOmittedCount = Array.isArray(rawDomainCapabilityLabels)
    ? Math.max(0, rawDomainCapabilityLabels.length - domainCapabilityLabels.length)
    : 0;
  const artifactFingerprintProjection = useMemo(
    () => buildRoomInspectorArtifactFingerprint(artifacts),
    [artifacts],
  );
  const actionDeskArtifactFingerprint = artifactFingerprintProjection.fingerprint;
  const projectionWarnings = [
    listProjection.members.hardLimited
      ? listProjection.members.hardOmittedCount + " 名成员超出安全投影上限"
      : "",
    listProjection.archivedMembers.hardLimited
      ? listProjection.archivedMembers.hardOmittedCount + " 名归档成员未投影"
      : "",
    listProjection.materials.hardLimited
      ? listProjection.materials.hardOmittedCount + " 条资料未投影"
      : "",
    providerIndex.projectionLimited
      ? providerIndex.omittedCount + " 个 Provider 未进入本地索引"
      : "",
    directorDecisionProjection.projectionLimited
      ? directorDecisionProjection.omittedCount + " 条较早主持决定未排序"
      : "",
    artifactFingerprintProjection.projectionLimited
      ? artifactFingerprintProjection.omittedCount + " 个较早工件未进入指纹"
      : "",
    workflowProjection.stageProjectionLimited || workflowProjection.coverageProjectionLimited
      ? "流程阶段或必须覆盖项超过本地展示上限"
      : "",
    walkForwardFootprint.projectionLimited
      ? "walk-forward 桶超过扫描上限，保守保留历史工作区"
      : "",
    domainCapabilityOmittedCount
      ? domainCapabilityOmittedCount + " 个房间能力标签未投影"
      : "",
  ].filter(Boolean);

  const runRoomAction = async (action, handler, fallback, ...args) => {
    if (roundActionRef.current || typeof handler !== "function") return false;
    roundActionRef.current = true;
    setPendingRoundAction(action);
    setRoundActionError("");
    try {
      await Promise.resolve(handler(...args));
      return true;
    } catch (error) {
      setRoundActionError(roomInspectorErrorMessage(error, fallback));
      return false;
    } finally {
      roundActionRef.current = false;
      setPendingRoundAction((current) => current === action ? "" : current);
    }
  };

  return (
    <aside className="room-inspector room-inspector-workbench" ref={inspectorRef}>
      <nav className="room-inspector-section-nav" aria-label="房间信息分区">
        <a href="#inspector-rooms">目标</a>
        <a href="#inspector-project-focus">结构</a>
        <a href="#inspector-convergence">门禁</a>
        <a href="#inspector-members">成员</a>
        <a href="#inspector-materials">资料</a>
        <a href="#inspector-artifacts">产物</a>
      </nav>
      <section className="inspector-section objective-section" id="inspector-rooms" tabIndex={-1} aria-label="本轮目标">
        <div className="section-heading"><strong>本轮目标</strong><span>{room?.category || "通用共创"}</span></div>
        <p>{room?.objective || "等待用户定义目标。"}</p>
        <div className="room-inspector-integrity-ledger" role="list" aria-label="检查面板本地投影状态">
          <span role="listitem">
            <small>ACTIVE MEMBERS</small>
            <strong>{listProjection.members.visibleCount} / {listProjection.members.sourceCount}</strong>
          </span>
          <span role="listitem">
            <small>MATERIALS</small>
            <strong>{listProjection.materials.visibleCount} / {listProjection.materials.sourceCount}</strong>
          </span>
          <span role="listitem">
            <small>STORAGE CONTRACT</small>
            <strong>{storageReadOnly ? "只读保留" : storageWorkspaceVisible ? "宿主可用" : "未接入"}</strong>
          </span>
        </div>
        {projectionWarnings.length ? (
          <div className="room-inspector-projection-note" role="note">
            <ShieldCheck size={14} aria-hidden="true" />
            <span><strong>本地投影上限已启用。</strong>{projectionWarnings.join("；")}。</span>
          </div>
        ) : null}
        <div className="room-capability-strip" aria-label="房间能力">
          {domainCapabilityLabels.map((label) => <span key={label}>{label}</span>)}
        </div>
        {stockResearchContribution?.present ? (
          <div
            className={`stock-research-contribution-status${stockResearchContribution.active ? " ready" : " blocked"}`}
            aria-label="股票只读宿主贡献状态"
          >
            <span>股票只读检查</span>
            <strong>{stockResearchContribution.active ? "合同已接入" : "只读阻断"}</strong>
            <small>
              {stockResearchContribution.active
                ? `显式股票池 ${stockRoomScopeSymbols.length} 个标的；检查与下一轮授权由宿主页面管理。`
                : stockResearchContribution.reason || roomInspectorSlot.reason || "当前冻结合同不可执行检查。"}
            </small>
          </div>
        ) : null}
        <div className={pausedRoundPending ? "round-controls pending-round" : "round-controls"}>
          <button
            className="primary"
            onClick={(event) => {
              void runRoomAction(
                "start",
                onStartRound,
                "开始讨论失败。",
                event.currentTarget,
              );
            }}
            disabled={
              roundControlBusy
              || routingBusy
              || endingRound
              || !canStart
              || typeof onStartRound !== "function"
            }
            aria-busy={pendingRoundAction === "start"}
            title={
              typeof onStartRound !== "function"
                ? "开始讨论处理器不可用"
                : !canStart ? startBlockReason : "检查模型执行器并开始新一轮"
            }
          >
            <Play size={15} />{pendingRoundAction === "start" ? "正在开始…" : "开始一轮"}
          </button>
          {roundState.running ? (
            <button
              className="secondary compact"
              onClick={() => {
                void runRoomAction("pause", onPause, "暂停讨论失败。");
              }}
              disabled={
                roundState.pausing
                || !roundState.roundId
                || Boolean(pendingRoundAction)
                || typeof onPause !== "function"
              }
              aria-busy={pendingRoundAction === "pause"}
              title="将在当前成员发言结束后的安全检查点暂停；已完成内容会保留。"
            ><Pause size={15} />{roundState.pausing || pendingRoundAction === "pause" ? "正在暂停…" : "暂停讨论"}</button>
          ) : pausedRoundPending ? (
            <span className={canResume ? "pending-round-actions" : "pending-round-actions single"}>
              {canResume ? (
                <button
                  className="secondary compact"
                  type="button"
                  onClick={() => {
                    void runRoomAction("resume", onResumeRound, "恢复暂停轮失败。");
                  }}
                  disabled={
                    routingBusy
                    || !providerReady
                    || endingRound
                    || Boolean(pendingRoundAction)
                    || typeof onResumeRound !== "function"
                  }
                  aria-busy={pendingRoundAction === "resume"}
                  title="沿用冻结证据截面，从最后成功检查点继续"
                >
                  <RotateCcw size={15} />{pendingRoundAction === "resume" ? "正在恢复…" : "继续上次"}
                </button>
              ) : null}
              {roundAvailability.canEnd ? (
                <button
                  className="secondary compact round-cancel-button"
                  type="button"
                  onClick={() => {
                    void runRoomAction("end", onEndRound, "结束暂停轮失败。");
                  }}
                  disabled={
                    endingRound
                    || routingBusy
                    || Boolean(pendingRoundAction)
                    || typeof onEndRound !== "function"
                  }
                  aria-busy={pendingRoundAction === "end"}
                  title="明确结束暂停轮；已完成消息和审计记录仍会保留"
                >
                  <X size={15} />{endingRound || pendingRoundAction === "end" ? "正在结束…" : "结束本轮"}
                </button>
              ) : null}
            </span>
          ) : <button className="secondary compact" disabled><Pause size={15} />暂停</button>}
        </div>
        {roundActionError ? (
          <div className="room-inspector-action-error" role="alert">
            <X size={14} aria-hidden="true" />{roundActionError}
          </div>
        ) : null}
        <div className={`meeting-readiness ${marketGate?.severity === "critical" ? "critical" : canStart && providerCheckStatus === "ready" ? "ready" : canStart ? "pending" : "blocked"}`}>
          <div className="screen-reader-announcer" role="status" aria-live="polite" aria-atomic="true">
            {meetingReadinessAnnouncementText({
              workflowReady,
              marketRequired: Boolean(marketGate?.required),
              marketReady: Boolean(marketGate?.ready),
              marketState: marketGate?.state,
              providerStatus: providerCheckStatus,
              reason: !canStart
                ? startBlockReason
                : providerCheckStatus === "failed"
                  ? providerPreflightState?.reason
                  : "",
            })}
          </div>
          <div className="meeting-readiness-row">
            <span>讨论角色与阶段</span>
            <strong>{workflowReady ? "已覆盖" : "有缺口"}</strong>
          </div>
          {marketGate?.required ? (
            <div className="meeting-readiness-row">
              <span>Futu 四股行情</span>
              <strong>{marketGate.ready ? "已就绪" : marketGate.state === "checking" ? "检查中" : "未就绪"}</strong>
            </div>
          ) : null}
          <div className="meeting-readiness-row">
            <span>模型执行器</span>
            <strong>
              {providerCheckStatus === "ready"
                ? "检查通过"
                : providerCheckStatus === "checking"
                  ? "检查中"
                  : providerCheckStatus === "failed" || providerCheckStatus === "blocked"
                    ? "未通过"
                    : "待检查"}
            </strong>
          </div>
          {!canStart ? <p className="meeting-readiness-reason">{startBlockReason}</p> : null}
          {providerCheckStatus === "failed" && providerPreflightState?.reason
            ? <p className="meeting-readiness-reason">{providerPreflightState.reason}</p>
            : null}
        </div>
        <div className={routeSummary.hasOpenAI ? "round-provider-preview warning" : "round-provider-preview"}>
          <Network size={13} />
          <span><small>本轮预计模型</small><strong>{routeSummary.label}</strong></span>
        </div>
        {pausedRoundPending && canResume ? (
          <div className="checkpoint-note">
            已保存到第 {pendingRoundCheckpoint.step_number || 0} 步 · 完成 {pendingRoundCheckpoint.completed || 0} 位成员。
            继续时沿用冻结截面{frozenMarket?.snapshot_id ? ` ${frozenMarket.snapshot_id}` : ""}，不会改用当前行情。
          </div>
        ) : pausedRoundPending ? (
          <div className="checkpoint-note warning">
            本轮缺少可恢复检查点，不能安全继续；你仍可明确结束本轮，已完成消息和审计记录不会删除。
          </div>
        ) : null}
        {!providerReady && <div className="provider-warning">{roundProviderBlockReason || "当前没有可用模型执行器"}</div>}
      </section>

      <ProjectRoundFocusCard
        room={room}
        members={boundedMembers}
        artifacts={artifacts}
        pendingRound={pendingRound}
        slot={roomInspectorSlot}
        contribution={projectRoundFocusContribution}
        authorization={roundFocusAuthorization}
        onFillObjective={onFillRoundFocusObjective}
      />

      <ConvergenceCard convergence={convergence} running={roundState.running} />

      <ProviderRoutingPanel
        room={room}
        members={boundedMembers}
        providers={providerIndex.rows}
        roundRunning={roundBusy}
        routingBusy={routingBusy}
        onRouteMembers={onRouteMembers}
        onRunPreflight={onRunProviderPreflight}
      />

      <section className="inspector-section workflow-summary-section" id="inspector-workflow">
        <div className="section-heading">
          <strong><SlidersHorizontal size={15} />讨论流程</strong>
          <button
            className="text-action"
            type="button"
            disabled={typeof onEditWorkflowPolicy !== "function"}
            onClick={() => onEditWorkflowPolicy?.()}
          >设置</button>
        </div>
        {workflowPolicy ? (
          <>
            <div className="workflow-summary-meta">
              <span className={templateDefault ? "policy-source-tag template" : "policy-source-tag custom"}>
                {templateDefault ? "模板默认" : "已自定义"}
              </span>
              {roundBusy ? <small>修改从下一轮生效</small> : null}
            </div>
            <div className="workflow-stage-path" aria-label="讨论阶段顺序">
              {workflowProjection.stageOrder.map((stage, index) => (
                <span key={stage}>
                  <b>{stageLabel(stage)}</b>
                  <small>至少 {workflowPolicy.minimum_stage_coverage?.[stage] || 1} 位</small>
                  {index < workflowProjection.stageOrder.length - 1 ? <i>→</i> : null}
                </span>
              ))}
            </div>
            <dl className="workflow-summary-facts">
              <div><dt>最低总覆盖</dt><dd>{workflowPolicy.minimum_successful_members} 位不同成员</dd></div>
              <div><dt>发言边界</dt><dd>每人最多 {workflowPolicy.max_turns_per_member} 次 · 追问 {workflowPolicy.follow_up_budget} 次</dd></div>
              <div><dt>必须覆盖</dt><dd title={requiredCoverage.map((item) => item.label).join("、")}>
                {requiredCoverage.length
                  ? `${requiredCoverage.slice(0, 3).map((item) => item.label).join("、")}${requiredCoverage.length > 3 ? ` 等 ${requiredCoverage.length} 项` : ""}`
                  : "未设置额外专业意见"}
              </dd></div>
            </dl>
            <div className={workflowConfiguration?.ready ? "workflow-configuration-state ready" : "workflow-configuration-state blocked"}>
              <strong>{workflowConfiguration?.ready ? "配置可执行" : "配置存在缺口"}</strong>
              <small>
                {workflowConfiguration?.ready
                  ? "当前启用成员能够覆盖全部阶段和必须职责。"
                  : workflowConfiguration?.blockers?.[0]?.detail || "请调整成员阶段、能力或讨论流程。"}
              </small>
            </div>
            <div className="workflow-safe-summary"><ShieldCheck size={14} />用户最终确认 · 仅研究、回测与模拟 · 无实盘执行</div>
          </>
        ) : <div className="director-decision-empty">房间流程尚未载入。</div>}
      </section>

      {storageWorkspaceVisible && storageReadOnly ? (
        <section className="inspector-section plugin-workspace-state read-only" role="note">
          <ShieldCheck size={15} />
          <span><strong>存储研究工作区只读</strong><small>{storageReadOnlyReason}</small></span>
        </section>
      ) : null}

      {storageContribution?.present || storageSampleAcceptance ? (
        <PluginActionBoundary disabled={storageReadOnly} label="存储产业样板验收">
          <Suspense fallback={<InspectorPanelFallback label="存储产业样板验收" />}>
            <StorageSampleAcceptanceCard acceptance={storageSampleAcceptance} />
          </Suspense>
        </PluginActionBoundary>
      ) : null}

      {decisionLineageVisible ? (
        <PluginActionBoundary disabled={storageReadOnly} label="决策研究谱系">
          <Suspense fallback={<InspectorPanelFallback label="决策研究谱系" />}>
            <section className="inspector-section compact-section" id="inspector-decision-lineage">
              <DecisionLineagePanel
                decisionPackages={decisionPackages}
                members={boundedMembers}
                paperPortfolios={paperPortfolios}
                observations={observations}
                walkForwardRunsByPortfolio={walkForwardRunsByPortfolio}
                onCreatePortfolio={onAddPaperPortfolioFromDecision}
                onCreateObservation={onAddObservationFromDecision}
                onBindObservation={onBindObservationDecisionLineage}
              />
            </section>
          </Suspense>
        </PluginActionBoundary>
      ) : null}

      <section className="inspector-section">
        <div className="section-heading">
          <strong>本轮动态调度</strong>
          <span>
            {currentRoundDirectorDecisions.length}{directorDecisionProjection.projectionLimited ? "+" : ""} 次 · {roundState.running && roundState.stage ? stageLabel(roundState.stage) : roundState.running ? "进行中" : "成员状态"}
          </span>
        </div>
        <RoundExecutionTraceSummary
          roundId={currentRoundId}
          roundStatus={currentRoundStatus}
          trace={visibleTraceState?.trace || null}
          loading={visibleTraceState?.loading || false}
          error={visibleTraceState?.error || ""}
          stale={visibleTraceState?.stale || false}
          onOpen={onOpenRoundExecutionTrace}
        />
        {directorDecision ? (
          <div className={`director-decision ${directorDecision.action === "finish" ? "finish" : "speak"}`} aria-live="polite">
            <div className="director-decision-head">
              <GitBranch size={14} />
              <span>{directorDecision.action === "finish" ? "主持建议" : "下一位"}</span>
              <strong>{directorDecision.action === "finish" ? "结束本轮并交由用户复核" : safeRoomInspectorText(directorDecision.member?.name, 160) || "等待成员"}</strong>
              <em>{directorSourceLabel(safeRoomInspectorText(directorDecision.source, 80))} · {stageLabel(safeRoomInspectorText(directorDecision.stage, 80) || "flexible")}</em>
            </div>
            <p>{safeRoomInspectorText(directorDecision.reason, 2000) || "未提供调度理由。"}</p>
            {directorDecision.workspaceFocus ? (
              <div className="director-workspace-focus">
                <span>正在补齐</span>
                <strong>{safeRoomInspectorText(directorDecision.workspaceFocus.title, 300) || "未命名工作区"}</strong>
              </div>
            ) : null}
            <DirectorModeratorAttribution context={directorDecision.moderatorContext} />
          </div>
        ) : (
          <div className="director-decision-empty">开始一轮后，这里会显示主持人选择下一位成员的理由与调度来源。</div>
        )}
        <ol className="speaker-order">
          {listProjection.members.visibleRows.filter((member) => member.enabled).map((member, index) => {
            const memberId = safeRoomInspectorText(member.id, 240);
            const memberName = safeRoomInspectorText(member.name, 160) || "未命名成员";
            const status = roundState.memberStatus?.[memberId] || "queued";
            return (
              <li key={(memberId || "speaker") + "-" + index} className={status}>
                <span className="speaker-state-dot" title={`安全回退顺序 ${index + 1}`} />
                <span className="mini-avatar" style={{ background: safeRoomInspectorColor(member.avatar_color) }}>{memberName.slice(0, 1)}</span>
                <span className="speaker-copy"><strong>{memberName}</strong><small>{statusText(status)}</small></span>
              </li>
            );
          })}
        </ol>
      </section>

      <section className="inspector-section" id="inspector-members" tabIndex={-1} aria-label="成员与身份">
        <div className="section-heading">
          <strong><Users size={15} />成员与身份</strong>
          <button
            className="text-action"
            type="button"
            disabled={typeof onAddMember !== "function"}
            onClick={() => onAddMember?.()}
          ><UserPlus size={14} />添加</button>
        </div>
        <div className="member-list">
          {listProjection.members.visibleRows.map((member, index) => {
            const memberId = safeRoomInspectorText(member.id, 240);
            const memberName = safeRoomInspectorText(member.name, 160) || "未命名成员";
            const memberProviderId = (
              safeRoomInspectorText(member.provider, 160) || "openai"
            ).toLowerCase();
            const memberProvider = providerIndex.providerMap.get(memberProviderId);
            const providerName = safeRoomInspectorText(memberProvider?.name, 160)
              || memberProviderId;
            const modelName = safeRoomInspectorText(member.model, 160)
              || safeRoomInspectorText(memberProvider?.model, 160)
              || "默认模型";
            return (
              <div key={(memberId || "member") + "-" + index} className={memberProviderId === "openai" ? "member-row uses-openai" : "member-row"}>
                <button
                  className="member-main"
                  type="button"
                  disabled={typeof onEditMember !== "function"}
                  onClick={() => onEditMember?.(member)}
                >
                  <span className="mini-avatar" style={{ background: safeRoomInspectorColor(member.avatar_color) }}>{memberName.slice(0, 1)}</span>
                  <span>
                    <strong>{memberName}</strong>
                    <small>{stageLabel(safeRoomInspectorText(member.workflow_stage, 80) || "flexible")} · {safeRoomInspectorText(member.identity, 300) || "未设置身份"}</small>
                    <small className={memberProviderId === "openai" ? "member-provider-line warning" : "member-provider-line"} title={`${providerName} · ${modelName}`}>
                      {providerName} · {modelName}
                    </small>
                  </span>
                  <i className={member.enabled ? "online" : "offline"} />
                </button>
                <span className="member-order-actions">
                  <button type="button" className="member-history-button" title="查看身份版本历史" aria-label={`查看${memberName}的身份版本历史`} disabled={typeof onViewMemberHistory !== "function"} onClick={() => onViewMemberHistory?.(member)}><History size={13} /></button>
                  <button type="button" title="向前移动" disabled={!memberId || index === 0 || roundControlBusy || typeof onMoveMember !== "function"} onClick={() => onMoveMember?.(memberId, -1)}><ArrowUp size={13} /></button>
                  <button type="button" title="向后移动" disabled={!memberId || index === listProjection.members.boundedRows.length - 1 || roundControlBusy || typeof onMoveMember !== "function"} onClick={() => onMoveMember?.(memberId, 1)}><ArrowDown size={13} /></button>
                </span>
              </div>
            );
          })}
        </div>
        {listProjection.members.moreAvailable ? (
          <button
            className="inspector-list-more"
            type="button"
            onClick={() => setMemberLimit((current) => Math.min(
              current + ROOM_INSPECTOR_MEMBER_STEP,
              ROOM_INSPECTOR_MEMBER_LIMIT,
            ))}
          >
            展开更多成员 · 当前 {listProjection.members.visibleCount} / {listProjection.members.boundedCount}
          </button>
        ) : null}
        {listProjection.archivedMembers.sourceCount ? (
          <details className="archived-member-list">
            <summary>已归档成员（{listProjection.archivedMembers.sourceCount}）</summary>
            <div>
              {listProjection.archivedMembers.visibleRows.map((member, index) => {
                const memberId = safeRoomInspectorText(member.id, 240);
                const memberName = safeRoomInspectorText(member.name, 160) || "未命名成员";
                return (
                <article key={(memberId || "archived-member") + "-" + index}>
                  <span className="mini-avatar" style={{ background: safeRoomInspectorColor(member.avatar_color) }}>{memberName.slice(0, 1)}</span>
                  <span><strong>{memberName}</strong><small>身份版本 v{Number.isSafeInteger(member.version) && member.version > 0 ? member.version : 1} · 历史记录保留</small></span>
                  <span className="archived-member-actions">
                    <button className="text-action" type="button" disabled={typeof onViewMemberHistory !== "function"} onClick={() => onViewMemberHistory?.(member)}><History size={13} />历史</button>
                    <button className="text-action" type="button" disabled={memberLifecycleLocked || typeof onRestoreMember !== "function"} title={memberLifecycleLocked ? "当前轮次运行或暂停中，结束后才能恢复成员" : "恢复为活动成员"} onClick={() => onRestoreMember?.(member)}><RotateCcw size={13} />恢复</button>
                  </span>
                </article>
                );
              })}
            </div>
            {listProjection.archivedMembers.moreAvailable ? (
              <button
                className="inspector-list-more"
                type="button"
                onClick={() => setArchivedMemberLimit((current) => Math.min(
                  current + ROOM_INSPECTOR_ARCHIVED_MEMBER_STEP,
                  ROOM_INSPECTOR_ARCHIVED_MEMBER_LIMIT,
                ))}
              >
                展开更多归档成员 · 当前 {listProjection.archivedMembers.visibleCount} / {listProjection.archivedMembers.boundedCount}
              </button>
            ) : null}
          </details>
        ) : null}
      </section>

      {paperPortfolioVisible ? (
        <PluginActionBoundary disabled={storageReadOnly} label="模拟组合与风险预算">
          <Suspense fallback={<InspectorPanelFallback label="模拟组合与风险预算" />}>
            <section className="inspector-section compact-section" id="inspector-paper-portfolio" tabIndex={-1}>
              <div className="section-heading"><strong><ShieldCheck size={15} />模拟组合与风险预算</strong><span>确定性复算</span></div>
              <PaperPortfolioPanel
                portfolios={paperPortfolios}
                loading={paperPortfolioLoading}
                walkForwardRunsByPortfolio={walkForwardRunsByPortfolio}
                walkForwardLoadingByPortfolio={walkForwardLoadingByPortfolio}
                walkForwardErrorsByPortfolio={walkForwardErrorsByPortfolio}
                candidateComparison={candidateComparison}
                candidateComparisonLoading={candidateComparisonLoading}
                candidateComparisonError={candidateComparisonError}
                decisionPackages={decisionPackages}
                onAdd={onAddPaperPortfolio}
                onEdit={onEditPaperPortfolio}
                onConfirm={onConfirmPaperPortfolio}
                onEvaluate={onEvaluatePaperPortfolio}
                onRunWalkForward={onRunPaperPortfolioWalkForward}
                onCompareCandidates={onCompareCandidates}
              />
            </section>
          </Suspense>
        </PluginActionBoundary>
      ) : null}

      {observationsVisible ? (
        <PluginActionBoundary disabled={storageReadOnly} label="模拟观察与验证">
          <Suspense fallback={<InspectorPanelFallback label="模拟观察与验证" />}>
            <section className="inspector-section compact-section" id="inspector-observations" tabIndex={-1}>
              <div className="section-heading"><strong><FlaskConical size={15} />模拟观察与验证</strong><span>1 / 5 / 20 日</span></div>
              <ObservationPanel
                observations={observations}
                reflections={reflections}
                scorecard={observationScorecard}
                loading={observationLoading}
                onAdd={onAddObservation}
                onConfirm={onConfirmObservation}
                onReconcile={onReconcileObservations}
                onEditReflection={onEditReflection}
              />
            </section>
          </Suspense>
        </PluginActionBoundary>
      ) : null}

      <section className="inspector-section compact-section" id="inspector-materials" tabIndex={-1} aria-label="共享资料">
        <div className="section-heading">
          <strong><Database size={15} />共享资料</strong>
          <button className="text-action" onClick={onAddMaterial}><FilePlus2 size={14} />添加</button>
        </div>
        {marketWorkspaceVisible ? (
          <PluginActionBoundary disabled={storageReadOnly} label="存储产业只读市场资料">
            <Suspense fallback={<InspectorPanelFallback label="存储产业只读市场资料" />}>
              <MarketSnapshotCard
                roomId={room?.id}
                snapshot={marketSnapshot}
                status={marketStatus}
                readiness={marketReadiness}
                loading={marketLoading}
                readinessLoading={marketReadinessLoading}
                gate={marketGate}
                onRefresh={onRefreshMarket}
                onRefreshReadiness={onRefreshMarketReadiness}
                onFreezeOfficialEvidence={onFreezeOfficialEvidence}
                onAddOfficialSupplement={onAddOfficialSupplement}
              />
            </Suspense>
          </PluginActionBoundary>
        ) : null}
        {listProjection.materials.sourceCount > 0 ? (
          <div className="material-list">
            {listProjection.materials.visibleRows.map((material, index) => {
              const quarantine = materialPromptQuarantine(material);
              const materialId = safeRoomInspectorText(material.id, 240);
              const materialTitle = safeRoomInspectorText(material.title, 300) || "未命名资料";
              return (
                <button
                  className={`material-row${quarantine.quarantined ? " quarantined" : ""}`}
                  type="button"
                  key={(materialId || "material") + "-" + index}
                  disabled={typeof onEditMaterial !== "function"}
                  onClick={() => onEditMaterial?.(material)}
                  title={quarantine.quarantined
                    ? `原文保留在本机，但不会发送给 AI。标记：${quarantine.labels.join("、")}`
                    : ""}
                >
                  <span><strong>{materialTitle}</strong><small>{materialSourceLabel(material)} · v{Number.isSafeInteger(material.version) && material.version > 0 ? material.version : 1}{material.metadata?.truncated ? " · 已截断" : ""}</small></span>
                  {safeRoomInspectorText(material.source_url, 4000) ? <Link2 size={13} /> : <span className="material-id">{materialId.slice(-5) || "LOCAL"}</span>}
                </button>
              );
            })}
          </div>
        ) : <div className="empty-resource">尚未添加可引用资料</div>}
        {listProjection.materials.moreAvailable ? (
          <button
            className="inspector-list-more"
            type="button"
            onClick={() => setMaterialLimit((current) => Math.min(
              current + ROOM_INSPECTOR_MATERIAL_STEP,
              ROOM_INSPECTOR_MATERIAL_LIMIT,
            ))}
          >
            展开更多资料 · 当前 {listProjection.materials.visibleCount} / {listProjection.materials.boundedCount}
          </button>
        ) : null}
      </section>

      <section className="inspector-section compact-section" id="inspector-artifacts" tabIndex={-1} aria-label="结论与待办">
        <div className="section-heading"><strong><CheckSquare size={15} />结论与待办</strong></div>
        <ArtifactPanel
          artifacts={artifacts}
          members={boundedMembers}
          loading={artifactLoading}
          generationDisabled={roundBusy}
          generationDisabledReason="本轮讨论仍在进行；请等待轮次结束后再整理，避免生成不完整纪要。"
          onGenerate={onGenerateArtifact}
          onEdit={onEditArtifact}
        />
      </section>

      <ActionDeskPanel
        roomId={room?.id}
        artifactFingerprint={actionDeskArtifactFingerprint}
        onFillComposer={onFillActionDeskComposer}
      />
    </aside>
  );
});
