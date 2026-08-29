import { Inbox, Menu, PanelLeft, Pause, Settings, Users, X } from "lucide-react";
import {
  Suspense,
  lazy,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  api,
  resumeChatRequest as streamResumeChatRequest,
  resumeRound as streamResumeRound,
  streamMessage,
  streamRound,
} from "./api";
import { downloadArtifactMarkdown } from "./artifacts";
import { buildArtifactUserDecisionRequest } from "./artifactUserDecision";
import { resolveComposerMentions } from "./composerMentions";
import {
  buildOfficialSupplementDraft,
  confirmedOfficialAttestationDialogState,
  fileToMaterialPayload,
} from "./materials";
import { deriveMarketGate } from "./marketGate";
import { createLatestRequestCoordinator } from "./latestRequest";
import { normalizeDirectorDecision } from "./directorDecision";
import {
  emptyDiscussionAuditState,
  normalizeDiscussionAudit,
} from "./discussionAudit";
import {
  emptyMessageSearchState,
  mergeMessageSearchPage,
  mergeUniqueHistoryRecords,
  messageHistoryStateFromSnapshot,
} from "./messageHistory";
import { hasRoomCapability, ROOM_CAPABILITIES } from "./roomCapabilities";
import {
  applyMaterialToRoomSnapshot,
  applyOfficialAttestationToRoomSnapshot,
  officialAttestationsFromRoomResponse,
} from "./roomSnapshot";
import { deriveRoundAvailability } from "./roundAvailability";
import {
  emptyRoundState,
  reconcileRoomRuntime,
  reduceRoomRuntimeEvent,
  roomRuntimeFor,
  shouldApplyRoomRefresh,
  updateSelectedRoomSnapshot,
  updateRoomRuntime,
} from "./roomRuntime";
import { ChatTimeline } from "./components/ChatTimeline";
import { Composer } from "./components/Composer";
import { IconRail } from "./components/IconRail";
import {
  normalizedProviderId,
  normalizeProviderPreflight,
  providerIsAvailable,
  UNASSIGNED_PROVIDER_ID,
} from "./providerRouting";
import { RoomSidebar } from "./components/RoomSidebar";
import { buildCandidateComparisonRequest } from "./candidateComparisonView";
import { buildWalkForwardRequestPayload } from "./walkForwardScenarioView";
import { workflowConfigurationGate } from "./workflowPolicy";
import { bindVisualViewportCssVars } from "./visualViewport";
import {
  createRoundLaunchRequestContext,
  PROVIDER_CALL_LIMIT_MAX,
  PROVIDER_CALL_LIMIT_MIN,
  roundLaunchPlanContextState,
} from "./roundLaunchPlan";
import {
  mergeRoundExecutionTracePages,
  normalizeRoundExecutionTrace,
} from "./roundExecutionTrace";
import {
  normalizeProjectRoundFocusAuthorization,
  projectRoundFocusArtifactFingerprint,
  projectRoundFocusAuthorizationState,
  projectRoundFocusRoomContextFingerprint,
} from "./projectRoundFocus";
import {
  footballResearchInspectorActivation,
  footballRoundContextAuthorizationState,
} from "./footballResearch.js";
import {
  stockResearchInspectorActivation,
  stockRoundContextAuthorizationState,
} from "./stockResearch.js";
import {
  buildRoundContextAuthorizationSet,
  roundContextAuthorizationEntry,
} from "./roundContexts.js";
import { DeferredSurfaceFallback } from "./DeferredSurfaceFallback.js";

const ActionOverviewDrawer = lazy(() => import("./components/ActionOverviewDrawer.jsx")
  .then((module) => ({ default: module.ActionOverviewDrawer })));
const ChatGPTCollaborationDialog = lazy(() => import("./components/ChatGPTCollaborationDialog.jsx")
  .then((module) => ({ default: module.ChatGPTCollaborationDialog })));
const ArtifactDialog = lazy(() => import("./components/ArtifactDialog.jsx")
  .then((module) => ({ default: module.ArtifactDialog })));
const CreateRoomDialog = lazy(() => import("./components/Dialogs.jsx")
  .then((module) => ({ default: module.CreateRoomDialog })));
const MaterialDialog = lazy(() => import("./components/Dialogs.jsx")
  .then((module) => ({ default: module.MaterialDialog })));
const MemberDialog = lazy(() => import("./components/Dialogs.jsx")
  .then((module) => ({ default: module.MemberDialog })));
const MemberVersionHistoryDialog = lazy(() => import("./components/MemberVersionHistoryDialog.jsx")
  .then((module) => ({ default: module.MemberVersionHistoryDialog })));
const ObservationDialog = lazy(() => import("./components/ObservationPanel.jsx")
  .then((module) => ({ default: module.ObservationDialog })));
const PaperPortfolioDialog = lazy(() => import("./components/PaperPortfolioPanel.jsx")
  .then((module) => ({ default: module.PaperPortfolioDialog })));
const ReflectionDialog = lazy(() => import("./components/ReflectionDialog.jsx")
  .then((module) => ({ default: module.ReflectionDialog })));
const RoomSettingsDialog = lazy(() => import("./components/RoomSettingsDialog.jsx")
  .then((module) => ({ default: module.RoomSettingsDialog })));
const RoundExecutionTraceDialog = lazy(() => import("./components/RoundExecutionTraceDialog.jsx")
  .then((module) => ({ default: module.RoundExecutionTraceDialog })));
const RoundLaunchDialog = lazy(() => import("./components/RoundLaunchDialog.jsx")
  .then((module) => ({ default: module.RoundLaunchDialog })));
const WorkflowPolicyDialog = lazy(() => import("./components/WorkflowPolicyDialog.jsx")
  .then((module) => ({ default: module.WorkflowPolicyDialog })));
const FootballResearchPanel = lazy(() => import("./components/FootballResearchPanel.jsx")
  .then((module) => ({ default: module.FootballResearchPanel })));
const StockResearchPanel = lazy(() => import("./components/StockResearchPanel.jsx")
  .then((module) => ({ default: module.StockResearchPanel })));
let sourceInboxModulePromise = null;
function loadSourceInboxPanel() {
  if (!sourceInboxModulePromise) {
    sourceInboxModulePromise = import("./components/SourceInboxPanel.jsx")
      .then((module) => ({ default: module.SourceInboxPanel }))
      .catch((error) => {
        sourceInboxModulePromise = null;
        throw error;
      });
  }
  return sourceInboxModulePromise;
}
function preloadSourceInboxPanel() {
  loadSourceInboxPanel().catch(() => {});
}
const SourceInboxPanel = lazy(loadSourceInboxPanel);
let roomInspectorModulePromise = null;
function loadRoomInspector() {
  if (!roomInspectorModulePromise) {
    roomInspectorModulePromise = import("./components/RoomInspector.jsx")
      .then((module) => ({ default: module.RoomInspector }))
      .catch((error) => {
        roomInspectorModulePromise = null;
        throw error;
      });
  }
  return roomInspectorModulePromise;
}
function preloadRoomInspector() {
  loadRoomInspector().catch(() => {});
}
const RoomInspector = lazy(loadRoomInspector);

const ROUND_LAUNCH_SKIP_PROVIDERS = Object.freeze(["openai"]);
const ROUND_TRACE_STALE_EVENT_TYPES = new Set([
  "round_started",
  "round_resumed",
  "director_decision",
  "speaker_started",
  "message",
  "speaker_failed",
  "speaker_skipped",
  "round_paused",
  "round_completed",
]);

function emptyRoundLaunchState() {
  return {
    status: "idle",
    roomId: "",
    roomSettingsVersion: 0,
    objective: "",
    clientRoundRequestId: "",
    plan: null,
    error: "",
  };
}

function emptyRoundExecutionTraceState(overrides = {}) {
  return {
    open: false,
    roomId: "",
    roundId: "",
    trace: null,
    loading: false,
    loadingMore: false,
    error: "",
    stale: false,
    ...overrides,
  };
}

function newClientRoundRequestId() {
  const uniquePart = globalThis.crypto?.randomUUID?.()
    || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `web-round-${uniquePart}`;
}

function withDirectorDecisions(activeState) {
  return activeState
    ? {
      ...activeState,
      director_decisions: activeState.director_decisions || [],
      decision_packages: activeState.decision_packages || [],
      pending_chat_requests: activeState.pending_chat_requests || [],
      archived_members: activeState.archived_members || [],
      message_history: activeState.message_history || { has_more: false, next_cursor: "" },
    }
    : activeState;
}

function mergeDirectorDecisions(currentDecisions, incomingDecision) {
  const byId = new Map(
    (currentDecisions || [])
      .filter((decision) => decision?.id)
      .map((decision) => [decision.id, decision]),
  );
  byId.set(incomingDecision.id, incomingDecision);
  return [...byId.values()].sort((left, right) => (
    new Date(left.created_at).getTime() - new Date(right.created_at).getTime()
    || (Number(left.sequence_no) || 0) - (Number(right.sequence_no) || 0)
    || String(left.id).localeCompare(String(right.id))
  ));
}

function appendUniqueMessage(messages, message) {
  if (!message?.id) return messages || [];
  const current = messages || [];
  if (current.some((item) => item.id === message.id)) return current;
  return [...current, message];
}

function applyArtifactUserDecisionResponse(current, roomId, data) {
  if (current?.room?.id !== roomId) return current;
  const acceptanceIncluded = Object.hasOwn(data, "storage_sample_acceptance");
  return {
    ...current,
    artifacts: (current.artifacts || []).map((item) => (
      item.id === data.artifact.id ? data.artifact : item
    )),
    convergence: data.convergence || current.convergence,
    ...(acceptanceIncluded
      ? { storage_sample_acceptance: data.storage_sample_acceptance ?? null }
      : {}),
  };
}

const DEFERRED_SURFACE_EXIT_MS = 240;

function useDeferredActivation(active) {
  const [activated, setActivated] = useState(Boolean(active));
  useEffect(() => {
    if (active) {
      setActivated(true);
      return undefined;
    }
    if (!activated) return undefined;
    const timer = globalThis.setTimeout(
      () => setActivated(false),
      DEFERRED_SURFACE_EXIT_MS,
    );
    return () => globalThis.clearTimeout(timer);
  }, [active, activated]);
  return Boolean(active) || activated;
}

function useStableCallback(callback) {
  const callbackRef = useRef(callback);
  useLayoutEffect(() => {
    callbackRef.current = callback;
  }, [callback]);
  return useCallback((...args) => callbackRef.current?.(...args), []);
}

const COMPACT_INSPECTOR_QUERY = "(max-width: 1180px)";
const MOBILE_ROOM_DRAWER_QUERY = "(max-width: 760px)";
const INSPECTOR_FOCUSABLE_SELECTOR = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  '[tabindex]:not([tabindex="-1"])',
].join(",");

function useMediaQuery(query) {
  const [matches, setMatches] = useState(() => globalThis.matchMedia?.(query).matches ?? false);
  useEffect(() => {
    const media = globalThis.matchMedia?.(query);
    if (!media) return undefined;
    const sync = () => setMatches(media.matches);
    sync();
    media.addEventListener("change", sync);
    return () => media.removeEventListener("change", sync);
  }, [query]);
  return matches;
}

export default function App() {
  useEffect(() => bindVisualViewportCssVars(), []);
  const [rooms, setRooms] = useState([]);
  const [active, setActive] = useState(null);
  const [providers, setProviders] = useState([]);
  const [templates, setTemplates] = useState([]);
  const [memberTemplates, setMemberTemplates] = useState([]);
  const [capabilityPacks, setCapabilityPacks] = useState([]);
  const [pluginRegistry, setPluginRegistry] = useState(null);
  const [pluginLifecycle, setPluginLifecycle] = useState(null);
  const [search, setSearch] = useState("");
  const [composer, setComposer] = useState("");
  const [composerMentions, setComposerMentions] = useState([]);
  const [messageHistory, setMessageHistory] = useState(() => messageHistoryStateFromSnapshot(null));
  const [messageSearchInput, setMessageSearchInput] = useState("");
  const [messageSearch, setMessageSearch] = useState(() => emptyMessageSearchState());
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [runtimeByRoomId, setRuntimeByRoomId] = useState({});
  const [createOpen, setCreateOpen] = useState(false);
  const [editingMember, setEditingMember] = useState(null);
  const [memberHistoryTarget, setMemberHistoryTarget] = useState(null);
  const [editingMaterial, setEditingMaterial] = useState(null);
  const [materialVersions, setMaterialVersions] = useState([]);
  const [materialVersionsLoading, setMaterialVersionsLoading] = useState(false);
  const [editingArtifact, setEditingArtifact] = useState(null);
  const [artifactLoading, setArtifactLoading] = useState(false);
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [roomDrawerOpen, setRoomDrawerOpen] = useState(false);
  const [actionOverviewOpen, setActionOverviewOpen] = useState(false);
  const [sourceInboxOpen, setSourceInboxOpen] = useState(false);
  const [chatGPTCollaborationOpen, setChatGPTCollaborationOpen] = useState(false);
  const [railSection, setRailSection] = useState("rooms");
  const [inspectorNavigation, setInspectorNavigation] = useState({
    targetId: "",
    requestId: 0,
  });
  const [marketSnapshot, setMarketSnapshot] = useState(null);
  const [marketStatus, setMarketStatus] = useState(null);
  const [marketReadiness, setMarketReadiness] = useState(null);
  const [marketLoading, setMarketLoading] = useState(false);
  const [marketReadinessLoading, setMarketReadinessLoading] = useState(false);
  const [observationOpen, setObservationOpen] = useState(false);
  const [observationLineageSource, setObservationLineageSource] = useState(null);
  const [observationLoading, setObservationLoading] = useState(false);
  const [editingReflection, setEditingReflection] = useState(null);
  const [editingPaperPortfolio, setEditingPaperPortfolio] = useState(null);
  const [paperPortfolioLoading, setPaperPortfolioLoading] = useState(false);
  const [walkForwardRunsByPortfolio, setWalkForwardRunsByPortfolio] = useState({});
  const [walkForwardLoadingByPortfolio, setWalkForwardLoadingByPortfolio] = useState({});
  const [walkForwardErrorsByPortfolio, setWalkForwardErrorsByPortfolio] = useState({});
  const [candidateComparison, setCandidateComparison] = useState(null);
  const [candidateComparisonLoading, setCandidateComparisonLoading] = useState(false);
  const [candidateComparisonError, setCandidateComparisonError] = useState("");
  const [workflowOpen, setWorkflowOpen] = useState(false);
  const [roomSettingsOpen, setRoomSettingsOpen] = useState(false);
  const [providerRoutingBusy, setProviderRoutingBusy] = useState(false);
  const [providerPreflightState, setProviderPreflightState] = useState({ status: "idle", reason: "" });
  const [roundLaunch, setRoundLaunch] = useState(() => emptyRoundLaunchState());
  const [roundFocusAuthorization, setRoundFocusAuthorization] = useState(null);
  const [footballRoundContextAuthorization, setFootballRoundContextAuthorization] = useState(null);
  const [stockRoundContextAuthorization, setStockRoundContextAuthorization] = useState(null);
  const [roundExecutionTrace, setRoundExecutionTrace] = useState(
    () => emptyRoundExecutionTraceState(),
  );
  const [discussionAudit, setDiscussionAudit] = useState(
    () => emptyDiscussionAuditState(),
  );
  const actionOverviewActivated = useDeferredActivation(actionOverviewOpen);
  const sourceInboxActivated = useDeferredActivation(sourceInboxOpen);
  const chatGPTCollaborationActivated = useDeferredActivation(chatGPTCollaborationOpen);
  const inspectorActivated = useDeferredActivation(inspectorOpen);
  const roundLaunchActivated = useDeferredActivation(roundLaunch.status !== "idle");
  const roundExecutionTraceActivated = useDeferredActivation(roundExecutionTrace.open);
  const dialogsActivated = useDeferredActivation(
    createOpen || Boolean(editingMember) || Boolean(editingMaterial),
  );
  const roomSettingsActivated = useDeferredActivation(roomSettingsOpen);
  const memberHistoryActivated = useDeferredActivation(Boolean(memberHistoryTarget));
  const workflowActivated = useDeferredActivation(workflowOpen);
  const artifactActivated = useDeferredActivation(Boolean(editingArtifact));
  const observationActivated = useDeferredActivation(observationOpen);
  const reflectionActivated = useDeferredActivation(Boolean(editingReflection));
  const paperPortfolioActivated = useDeferredActivation(Boolean(editingPaperPortfolio));
  const compactInspector = useMediaQuery(COMPACT_INSPECTOR_QUERY);
  const mobileRoomDrawer = useMediaQuery(MOBILE_ROOM_DRAWER_QUERY);
  const roundStreamControlsRef = useRef(new Map());
  const roundLaunchControlRef = useRef({
    sequence: 0,
    controller: null,
    context: null,
    plan: null,
    phase: "idle",
  });
  const pendingMessagesRef = useRef(new Map());
  const roundExecutionTraceRequestRef = useRef({ sequence: 0, controller: null });
  const discussionAuditRequestRef = useRef({ sequence: 0, controller: null });
  const roomLoadRequestRef = useRef(0);
  const activeRoomIdRef = useRef("");
  const providerOperationRef = useRef(false);
  const walkForwardOperationRef = useRef(new Set());
  const candidateComparisonRequestRef = useRef(null);
  const candidateComparisonContextRef = useRef("");
  const artifactConfirmRef = useRef(false);
  const materialVersionsRequestRef = useRef(0);
  const marketReadinessRequestRef = useRef(null);
  const storageRoomIdRef = useRef("");
  const inspectorWrapRef = useRef(null);
  const inspectorToggleRef = useRef(null);
  const inspectorCloseRef = useRef(null);
  const inspectorRestoreFocusRef = useRef(null);
  const inspectorPostCloseFocusRef = useRef(null);
  const inspectorWasOpenRef = useRef(false);
  const mobileRoomToggleRef = useRef(null);
  const roomDrawerRestoreFocusRef = useRef(null);
  const roundLaunchRestoreFocusRef = useRef(null);
  const chatGPTCollaborationRestoreFocusRef = useRef(null);
  const sourceInboxRestoreFocusRef = useRef(null);
  const roundLaunchSuccessFocusRef = useRef(null);
  const observationRestoreFocusRef = useRef(null);
  const reflectionRestoreFocusRef = useRef(null);
  const artifactRestoreFocusRef = useRef(null);
  const paperPortfolioRestoreFocusRef = useRef(null);
  const roundLaunchOpenRef = useRef(false);
  if (!marketReadinessRequestRef.current) {
    marketReadinessRequestRef.current = createLatestRequestCoordinator();
  }
  if (!candidateComparisonRequestRef.current) {
    candidateComparisonRequestRef.current = createLatestRequestCoordinator();
  }

  const room = active?.room || null;
  const activeRoomId = String(room?.id || "");
  activeRoomIdRef.current = activeRoomId;
  const activeRuntime = roomRuntimeFor(runtimeByRoomId, activeRoomId);
  const roundState = activeRuntime.roundState;
  const typingMember = activeRuntime.typingMember;
  const transientErrors = activeRuntime.transientErrors;
  const messageNotice = activeRuntime.messageNotice;
  const messageSending = activeRuntime.messageSending;
  const roundCancelBusy = activeRuntime.roundCancelBusy;
  const runtimeError = activeRuntime.streamError;
  const visibleError = runtimeError || error;

  const updateRuntime = (roomId, updater) => {
    setRuntimeByRoomId((current) => updateRoomRuntime(current, roomId, updater));
  };
  const setRoundStateForRoom = (roomId, value) => {
    updateRuntime(roomId, (runtime) => ({
      ...runtime,
      roundState: typeof value === "function" ? value(runtime.roundState) : value,
    }));
  };
  const setRuntimeField = (roomId, field, value) => {
    updateRuntime(roomId, (runtime) => ({
      ...runtime,
      [field]: typeof value === "function" ? value(runtime[field]) : value,
    }));
  };
  const setMessageNotice = (value) => setRuntimeField(activeRoomId, "messageNotice", value);
  const updateActiveRoom = (roomId, updater) => {
    setActive((current) => updateSelectedRoomSnapshot(current, roomId, updater));
  };

  const streamControlFor = (roomId) => {
    if (!roundStreamControlsRef.current.has(roomId)) {
      roundStreamControlsRef.current.set(roomId, {
        controller: null,
        roundId: "",
        pauseRequested: false,
        terminalSeen: false,
        transition: "",
      });
    }
    return roundStreamControlsRef.current.get(roomId);
  };
  const discardRoundLaunch = () => {
    const launchControl = roundLaunchControlRef.current;
    launchControl.controller?.abort();
    const launchRoomId = String(launchControl.context?.roomId || "");
    const streamControl = roundStreamControlsRef.current.get(launchRoomId);
    if (streamControl?.transition === "plan") streamControl.transition = "";
    roundLaunchControlRef.current = {
      sequence: launchControl.sequence + 1,
      controller: null,
      context: null,
      plan: null,
      phase: "idle",
    };
    setRoundLaunch(emptyRoundLaunchState());
  };
  const members = active?.members || [];
  const archivedMembers = active?.archived_members || [];
  const messages = active?.messages || [];
  const materials = active?.materials || [];
  const officialAttestations = active?.official_attestations || [];
  const artifacts = active?.artifacts || [];
  const roundFocusArtifactFingerprint = useMemo(
    () => projectRoundFocusArtifactFingerprint(artifacts),
    [artifacts],
  );
  const roundFocusRoomContextFingerprint = useMemo(
    () => projectRoundFocusRoomContextFingerprint({ room, members }),
    [members, room],
  );
  const roundFocusRequired = Boolean(
    (room?.capability_pack_ids || []).includes("project_round_focus")
    || (room?.plugin_registry_snapshot?.selected_capability_pack_ids || [])
      .includes("project_round_focus"),
  );
  const activeRoundFocusAuthorization = projectRoundFocusAuthorizationState(
    roundFocusAuthorization,
    {
      roomId: activeRoomId,
      artifactFingerprint: roundFocusArtifactFingerprint,
      roomContextFingerprint: roundFocusRoomContextFingerprint,
      pluginRegistrySnapshotSha256: room?.plugin_registry_snapshot_sha256,
    },
  );
  const observations = active?.observations || [];
  const reflections = active?.reflections || [];
  const paperPortfolios = active?.paper_portfolios || [];
  const decisionPackages = active?.decision_packages || [];
  const pendingIdleChatRequests = (active?.pending_chat_requests || []).filter(
    (request) => request.kind === "idle_mention" && ["PENDING", "PROCESSING"].includes(request.status),
  );
  const lineagePortfolioIds = useMemo(() => new Set(
    decisionPackages.flatMap((decisionPackage) => (
      Array.isArray(decisionPackage.lineage)
        ? decisionPackage.lineage
          .filter((event) => event.resource_type === "simulation.paper_portfolio")
          .map((event) => String(event.resource_id || ""))
          .filter(Boolean)
        : []
    )),
  ), [decisionPackages]);
  const paperPortfolioFingerprint = useMemo(
    () => [...new Map(
      paperPortfolios
        .filter((portfolio, index) => index < 4 || lineagePortfolioIds.has(String(portfolio.id)))
        .map((portfolio) => [String(portfolio.id), portfolio]),
    ).values()]
      .map((portfolio) => `${portfolio.id}:${portfolio.version}`)
      .sort()
      .join("|"),
    [lineagePortfolioIds, paperPortfolios],
  );
  const observationScorecard = active?.observation_scorecard || {};
  const directorDecisions = active?.director_decisions || [];
  const latestRound = active?.latest_round || null;
  const roundAvailability = deriveRoundAvailability(active);
  const pendingRound = roundAvailability.pendingRound;
  const pendingRoundCheckpoint = roundAvailability.pendingRoundCheckpoint;
  const footballResearchActivation = useMemo(
    () => footballResearchInspectorActivation({
      frozenContext: pendingRound || room,
      runtimeContext: room,
      pluginRegistry,
      pluginLifecycle,
    }),
    [pendingRound, pluginLifecycle, pluginRegistry, room],
  );
  useEffect(() => {
    setFootballRoundContextAuthorization((current) => {
      if (!current) return null;
      const sameRoom = String(current.room_id || "") === activeRoomId;
      const sameRegistry = String(current.plugin_registry_snapshot_sha256 || "").toLowerCase()
        === String(footballResearchActivation.slot?.snapshotSha256 || "").toLowerCase();
      return sameRoom && sameRegistry && footballResearchActivation.active ? current : null;
    });
  }, [
    activeRoomId,
    footballResearchActivation.active,
    footballResearchActivation.slot?.snapshotSha256,
  ]);
  const footballRoundContextRequired = Boolean(
    (room?.capability_pack_ids || []).includes("football_research_readonly")
    || (room?.plugin_registry_snapshot?.selected_capability_pack_ids || [])
      .includes("football_research_readonly"),
  );
  const activeFootballRoundContextAuthorization = footballRoundContextAuthorizationState(
    footballRoundContextAuthorization,
    {
      roomId: activeRoomId,
      pluginRegistrySnapshotSha256: room?.plugin_registry_snapshot_sha256,
    },
  );
  const stockResearchActivation = useMemo(
    () => stockResearchInspectorActivation({
      frozenContext: pendingRound || room,
      runtimeContext: room,
      pluginRegistry,
      pluginLifecycle,
    }),
    [pendingRound, pluginLifecycle, pluginRegistry, room],
  );
  useEffect(() => {
    setStockRoundContextAuthorization((current) => {
      if (!current) return null;
      const sameRoom = String(current.room_id || "") === activeRoomId;
      const sameRegistry = String(current.plugin_registry_snapshot_sha256 || "").toLowerCase()
        === String(stockResearchActivation.slot?.snapshotSha256 || "").toLowerCase();
      const sameScope = String(current.stock_room_scope_sha256 || "").toLowerCase()
        === String(room?.stock_room_scope_sha256 || "").toLowerCase();
      return sameRoom && sameRegistry && sameScope && stockResearchActivation.active
        ? current
        : null;
    });
  }, [
    activeRoomId,
    room?.stock_room_scope_sha256,
    stockResearchActivation.active,
    stockResearchActivation.slot?.snapshotSha256,
  ]);
  const stockRoundContextRequired = Boolean(
    (room?.capability_pack_ids || []).includes("stock_research_readonly")
    || (room?.plugin_registry_snapshot?.selected_capability_pack_ids || [])
      .includes("stock_research_readonly"),
  );
  const activeStockRoundContextAuthorization = stockRoundContextAuthorizationState(
    stockRoundContextAuthorization,
    {
      roomId: activeRoomId,
      stockRoomScopeSha256: room?.stock_room_scope_sha256,
      pluginRegistrySnapshotSha256: room?.plugin_registry_snapshot_sha256,
    },
  );
  const activeRoundContextAuthorizations = useMemo(() => {
    if (!roundFocusRequired && !footballRoundContextRequired && !stockRoundContextRequired) {
      return null;
    }
    const entries = [];
    if (roundFocusRequired && activeRoundFocusAuthorization.valid) {
      entries.push(roundContextAuthorizationEntry(
        "project_round_focus",
        "core.round.context/v1",
        activeRoundFocusAuthorization.request,
      ));
    }
    if (
      footballRoundContextRequired
      && activeFootballRoundContextAuthorization.valid
    ) {
      const authorization = activeFootballRoundContextAuthorization.authorization;
      entries.push(roundContextAuthorizationEntry(
        "football_research_readonly",
        "core.football.match_context/v1",
        {
          version: "football_round_context_request_v1",
          payload: authorization.contract,
          authorization: {
            version: "football_round_context_authorization_v1",
            owner_pack_id: "football_research_readonly",
            port_id: "core.football.match_context/v1",
            contract_sha256: authorization.contract_sha256,
            data_cutoff_utc: authorization.data_cutoff_utc,
            match_id: authorization.match_id,
            user_confirmed: true,
          },
        },
      ));
    }
    if (stockRoundContextRequired && activeStockRoundContextAuthorization.valid) {
      const authorization = activeStockRoundContextAuthorization.authorization;
      entries.push(roundContextAuthorizationEntry(
        "stock_research_readonly",
        "core.market.readonly_context/v1",
        {
          version: "stock_round_context_request_v1",
          payload: authorization.contract,
          authorization: {
            version: "stock_round_context_authorization_v1",
            owner_pack_id: "stock_research_readonly",
            port_id: "core.market.readonly_context/v1",
            contract_sha256: authorization.contract_sha256,
            stock_room_scope_sha256: authorization.stock_room_scope_sha256,
            data_cutoff_utc: authorization.data_cutoff_utc,
            user_confirmed: true,
          },
        },
      ));
    }
    return buildRoundContextAuthorizationSet(entries);
  }, [
    activeFootballRoundContextAuthorization.authorization,
    activeFootballRoundContextAuthorization.valid,
    activeRoundFocusAuthorization.request,
    activeRoundFocusAuthorization.valid,
    activeStockRoundContextAuthorization.authorization,
    activeStockRoundContextAuthorization.valid,
    footballRoundContextRequired,
    roundFocusRequired,
    stockRoundContextRequired,
  ]);
  const convergence = active?.convergence || null;
  const storageSampleAcceptance = active?.storage_sample_acceptance || null;
  const providerRouteFingerprint = useMemo(
    () => [
      room?.id || "",
      ...members
        .filter((member) => member.enabled)
        .map((member) => `${member.id}:${member.provider || ""}:${member.model || ""}`)
        .sort(),
      ...providers
        .map((provider) => (
          `${normalizedProviderId(provider.id)}:${provider.configured === true}:${provider.policy_disabled === true}:${provider.model || ""}`
        ))
        .sort(),
    ].join("|"),
    [members, providers, room?.id],
  );
  const roundProviderBlockReason = useMemo(() => {
    const enabledMembers = members.filter((member) => member.enabled);
    if (!enabledMembers.length) return "当前房间没有启用成员。";
    const providerById = new Map(
      providers.map((provider) => [normalizedProviderId(provider.id), provider]),
    );
    const unassignedMembers = enabledMembers.filter(
      (member) => normalizedProviderId(member.provider) === UNASSIGNED_PROVIDER_ID,
    );
    if (unassignedMembers.length) return `${unassignedMembers.length} 位成员尚未分配模型执行器。`;
    const policyDisabledMembers = enabledMembers.filter((member) => (
      providerById.get(normalizedProviderId(member.provider))?.policy_disabled === true
    ));
    if (policyDisabledMembers.length) {
      return `${policyDisabledMembers.length} 位成员使用了服务端策略禁用的模型执行器，请先迁移。`;
    }
    const unavailable = enabledMembers.filter((member) => {
      const provider = providerById.get(normalizedProviderId(member.provider));
      return !providerIsAvailable(provider);
    });
    if (unavailable.length) return `${unavailable.length} 位成员的模型执行器不存在或尚未配置。`;
    return "";
  }, [members, providers]);
  const workflowConfiguration = useMemo(
    () => workflowConfigurationGate(room?.workflow_policy, members),
    [members, room?.workflow_policy],
  );
  const workflowBlockReason = workflowConfiguration.ready
    ? ""
    : `讨论配置不可执行：${workflowConfiguration.blockers[0]?.title || "存在成员缺口"}。${workflowConfiguration.blockers[0]?.detail || ""}`;
  const roomTemplate = useMemo(
    () => templates.find((template) => template.id === room?.template_id) || null,
    [room?.template_id, templates],
  );
  const storageRoomId = hasRoomCapability(room, ROOM_CAPABILITIES.storageMarket) ? room.id : "";
  storageRoomIdRef.current = storageRoomId;
  candidateComparisonContextRef.current = `${storageRoomId}|${paperPortfolioFingerprint}`;
  const decisionLineageEnabled = hasRoomCapability(room, ROOM_CAPABILITIES.paperPortfolio)
    && hasRoomCapability(room, ROOM_CAPABILITIES.observations);
  const marketGate = useMemo(
    () => deriveMarketGate({
      required: Boolean(storageRoomId),
      snapshot: marketSnapshot,
      loading: marketLoading,
    }),
    [marketLoading, marketSnapshot, storageRoomId],
  );
  const canAttemptNewRound = !roundCancelBusy
    && !roundAvailability.hasPendingRound
    && workflowConfiguration.ready
    && marketGate.ready
    && (!roundFocusRequired || activeRoundFocusAuthorization.valid)
    && (
      !footballRoundContextRequired
      || activeFootballRoundContextAuthorization.valid
    )
    && (!stockRoundContextRequired || activeStockRoundContextAuthorization.valid);
  const newRoundBlockReason = (roundCancelBusy ? "正在结束暂停轮次，请稍候。" : "")
    || roundAvailability.blockReason
    || workflowBlockReason
    || marketGate.reason
    || (roundFocusRequired && !activeRoundFocusAuthorization.valid
      ? "请先在房间信息中读取并显式填入下一轮项目焦点。"
      : "")
    || (footballRoundContextRequired && !activeFootballRoundContextAuthorization.valid
      ? "请先在足球只读检查器中核验合同并显式授权用于下一轮。"
      : "")
    || (stockRoundContextRequired && !activeStockRoundContextAuthorization.valid
      ? "请先在股票只读检查器中核验房间股票池合同并显式授权用于下一轮。"
      : "");
  const roundBusy = roundState.running || roundState.pausing;
  const roundLaunchOpen = roundLaunch.status !== "idle";
  roundLaunchOpenRef.current = roundLaunchOpen;
  const memberLifecycleLocked = roundBusy || roundAvailability.hasPendingRound;
  const roundStatusLabel = roundState.pausing
    ? "正在暂停"
    : roundState.running
      ? "进行中"
      : roundAvailability.pausedRoundPending
        ? "已暂停"
        : roundAvailability.hasPendingRound
          ? "未结束"
        : "待讨论";

  const resetMessageNavigation = (snapshot) => {
    const nextSnapshot = snapshot || null;
    setMessageHistory(messageHistoryStateFromSnapshot(nextSnapshot));
    setMessageSearchInput("");
    setMessageSearch(emptyMessageSearchState(nextSnapshot?.room?.id));
  };

  useEffect(() => {
    api.bootstrap()
      .then((data) => {
        setRooms(data.rooms || []);
        const nextActive = withDirectorDecisions(data.active);
        const nextRoomId = String(nextActive?.room?.id || "");
        activeRoomIdRef.current = nextRoomId;
        setActive(nextActive);
        resetMessageNavigation(nextActive);
        if (nextRoomId) {
          setRuntimeByRoomId((current) => updateRoomRuntime(
            current,
            nextRoomId,
            (runtime) => reconcileRoomRuntime(runtime, nextActive),
          ));
        }
        setProviders(data.providers || []);
        setTemplates(data.templates || []);
        setMemberTemplates(data.member_templates || []);
        setCapabilityPacks(data.capability_packs || []);
        setPluginRegistry(data.plugin_registry || null);
        setPluginLifecycle(Object.hasOwn(data, "plugin_lifecycle") ? data.plugin_lifecycle : null);
      })
      .catch((requestError) => setError(requestError.message))
      .finally(() => setLoading(false));
    return () => {
      roundLaunchControlRef.current.controller?.abort();
      roundExecutionTraceRequestRef.current.controller?.abort();
      discussionAuditRequestRef.current.controller?.abort();
      marketReadinessRequestRef.current?.cancel();
      candidateComparisonRequestRef.current?.cancel();
      for (const control of roundStreamControlsRef.current.values()) {
        control.controller?.abort();
      }
    };
  }, []);

  useEffect(() => {
    if (!roomDrawerOpen && !inspectorOpen && !actionOverviewOpen) return undefined;
    const closeDrawers = (event) => {
      if (event.key !== "Escape") return;
      if (roundLaunchOpenRef.current) return;
      setRoomDrawerOpen(false);
      setInspectorOpen(false);
      setActionOverviewOpen(false);
    };
    window.addEventListener("keydown", closeDrawers);
    return () => window.removeEventListener("keydown", closeDrawers);
  }, [actionOverviewOpen, inspectorOpen, roomDrawerOpen]);

  useEffect(() => {
    if (!inspectorOpen) {
      if (!inspectorWasOpenRef.current) return undefined;
      inspectorWasOpenRef.current = false;
      const restoreFrame = globalThis.requestAnimationFrame(() => {
        const postCloseFocusTarget = inspectorPostCloseFocusRef.current;
        inspectorPostCloseFocusRef.current = null;
        const restoreTarget = [
          postCloseFocusTarget,
          inspectorRestoreFocusRef.current,
          inspectorToggleRef.current,
          mobileRoomToggleRef.current,
        ].find((target) => (
          target?.isConnected
          && target.disabled !== true
          && target.getClientRects().length > 0
        ));
        restoreTarget?.focus({ preventScroll: restoreTarget !== postCloseFocusTarget });
      });
      return () => globalThis.cancelAnimationFrame(restoreFrame);
    }

    inspectorWasOpenRef.current = true;
    if (!compactInspector) return undefined;
    const inspector = inspectorWrapRef.current;
    if (!inspector) return undefined;

    const initialFocusFrame = globalThis.requestAnimationFrame(() => {
      inspectorCloseRef.current?.focus({ preventScroll: true });
    });
    const trapFocus = (event) => {
      if (event.key !== "Tab") return;
      if (roundLaunchOpenRef.current) return;
      const controls = Array.from(inspector.querySelectorAll(INSPECTOR_FOCUSABLE_SELECTOR))
        .filter((element) => element.getClientRects().length > 0);
      if (!controls.length) {
        event.preventDefault();
        inspectorCloseRef.current?.focus({ preventScroll: true });
        return;
      }
      const first = controls[0];
      const last = controls.at(-1);
      const activeElement = document.activeElement;
      if (!inspector.contains(activeElement)) {
        event.preventDefault();
        (event.shiftKey ? last : first).focus({ preventScroll: true });
      } else if (event.shiftKey && activeElement === first) {
        event.preventDefault();
        last.focus({ preventScroll: true });
      } else if (!event.shiftKey && activeElement === last) {
        event.preventDefault();
        first.focus({ preventScroll: true });
      }
    };
    window.addEventListener("keydown", trapFocus);
    return () => {
      globalThis.cancelAnimationFrame(initialFocusFrame);
      window.removeEventListener("keydown", trapFocus);
    };
  }, [compactInspector, inspectorOpen]);

  useEffect(() => {
    setMemberHistoryTarget(null);
    const request = roundExecutionTraceRequestRef.current;
    request.controller?.abort();
    roundExecutionTraceRequestRef.current = {
      sequence: request.sequence + 1,
      controller: null,
    };
    setRoundExecutionTrace(emptyRoundExecutionTraceState());
    const auditRequest = discussionAuditRequestRef.current;
    auditRequest.controller?.abort();
    discussionAuditRequestRef.current = {
      sequence: auditRequest.sequence + 1,
      controller: null,
    };
    setDiscussionAudit(emptyDiscussionAuditState());
  }, [room?.id]);

  useEffect(() => {
    setRoundFocusAuthorization((current) => (
      projectRoundFocusAuthorizationState(current, {
        roomId: activeRoomId,
        artifactFingerprint: roundFocusArtifactFingerprint,
        roomContextFingerprint: roundFocusRoomContextFingerprint,
        pluginRegistrySnapshotSha256: room?.plugin_registry_snapshot_sha256,
      }).valid
        ? current
        : null
    ));
  }, [
    activeRoomId,
    room?.plugin_registry_snapshot_sha256,
    roundFocusArtifactFingerprint,
    roundFocusRoomContextFingerprint,
  ]);

  useEffect(() => {
    marketReadinessRequestRef.current.cancel();
    if (!storageRoomId) {
      setMarketSnapshot(null);
      setMarketStatus(null);
      setMarketReadiness(null);
      setMarketLoading(false);
      setMarketReadinessLoading(false);
      return undefined;
    }
    const controller = new AbortController();
    setMarketLoading(true);
    setMarketReadiness(null);
    Promise.allSettled([
      api.storageSnapshot(false, controller.signal),
      api.storageStatus(controller.signal),
    ])
      .then(([snapshotResult, statusResult]) => {
        if (snapshotResult.status === "fulfilled") setMarketSnapshot(snapshotResult.value.snapshot);
        if (statusResult.status === "fulfilled") setMarketStatus(statusResult.value.status);
        const requestError = [snapshotResult, statusResult]
          .find((result) => result.status === "rejected" && result.reason?.name !== "AbortError")
          ?.reason;
        if (requestError) setError(requestError.message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setMarketLoading(false);
      });
    return () => controller.abort();
  }, [storageRoomId]);

  useEffect(() => {
    const portfolioIds = paperPortfolioFingerprint
      .split("|")
      .map((entry) => entry.split(":")[0])
      .filter(Boolean);
    candidateComparisonRequestRef.current.cancel();
    setCandidateComparison(null);
    setCandidateComparisonError("");
    setWalkForwardRunsByPortfolio({});
    setWalkForwardErrorsByPortfolio({});
    if (!storageRoomId || !portfolioIds.length) {
      setWalkForwardLoadingByPortfolio({});
      return undefined;
    }

    let cancelled = false;
    setWalkForwardLoadingByPortfolio(
      Object.fromEntries(portfolioIds.map((portfolioId) => [portfolioId, true])),
    );
    Promise.all(portfolioIds.map(async (portfolioId) => {
      try {
        const data = await api.paperPortfolioWalkForwardRuns(storageRoomId, portfolioId);
        return {
          portfolioId,
          runs: Array.isArray(data.walk_forward_runs) ? data.walk_forward_runs : [],
          error: "",
        };
      } catch (requestError) {
        return { portfolioId, runs: [], error: requestError.message };
      }
    })).then((records) => {
      if (cancelled) return;
      setWalkForwardRunsByPortfolio(
        Object.fromEntries(records.map((record) => [record.portfolioId, record.runs])),
      );
      setWalkForwardErrorsByPortfolio(
        Object.fromEntries(
          records
            .filter((record) => record.error)
            .map((record) => [record.portfolioId, record.error]),
        ),
      );
      setWalkForwardLoadingByPortfolio({});
    });
    return () => {
      cancelled = true;
    };
  }, [paperPortfolioFingerprint, storageRoomId]);

  useEffect(() => {
    setProviderPreflightState({ status: "idle", reason: "" });
  }, [providerRouteFingerprint]);

  const loadRoom = async (roomId) => {
    if (
      roundLaunchControlRef.current.context
      && roundLaunchControlRef.current.context.roomId !== String(roomId)
    ) discardRoundLaunch();
    const requestId = roomLoadRequestRef.current + 1;
    roomLoadRequestRef.current = requestId;
    setError("");
    try {
      const data = await api.room(roomId);
      if (roomLoadRequestRef.current !== requestId) return false;
      const nextActive = {
        room: data.room,
        members: data.members,
        archived_members: data.archived_members || [],
        messages: data.messages,
        message_history: data.message_history || { has_more: false, next_cursor: "" },
        materials: data.materials || [],
        official_attestations: officialAttestationsFromRoomResponse(data),
        artifacts: data.artifacts || [],
        observations: data.observations || [],
        reflections: data.reflections || [],
        paper_portfolios: data.paper_portfolios || [],
        decision_packages: data.decision_packages || [],
        observation_scorecard: data.observation_scorecard || {},
        director_decisions: data.director_decisions || [],
        latest_round: data.latest_round,
        round_checkpoint: data.round_checkpoint || null,
        pending_round: data.pending_round || null,
        pending_round_checkpoint: data.pending_round_checkpoint || null,
        pending_chat_requests: data.pending_chat_requests || [],
        convergence: data.convergence || null,
        storage_sample_acceptance: data.storage_sample_acceptance || null,
      };
      activeRoomIdRef.current = String(roomId);
      setActive(nextActive);
      resetMessageNavigation(nextActive);
      setRuntimeByRoomId((current) => updateRoomRuntime(
        current,
        roomId,
        (runtime) => reconcileRoomRuntime(runtime, nextActive, {
          preserveLocalRunning: Boolean(
            roundStreamControlsRef.current.get(String(roomId))?.controller,
          ),
        }),
      ));
      setProviders(data.providers || providers);
      setComposer("");
      setComposerMentions([]);
      return true;
    } catch (requestError) {
      if (roomLoadRequestRef.current === requestId) setError(requestError.message);
      return false;
    }
  };

  const refreshRooms = async (roomId = activeRoomIdRef.current, { select = false } = {}) => {
    const targetRoomId = String(roomId || "");
    const data = await api.bootstrap(targetRoomId);
    setRooms(data.rooms || []);
    if (data.active) {
      const nextActive = withDirectorDecisions(data.active);
      const nextRoomId = String(nextActive?.room?.id || targetRoomId);
      setRuntimeByRoomId((current) => updateRoomRuntime(
        current,
        nextRoomId,
        (runtime) => reconcileRoomRuntime(runtime, nextActive, {
          preserveLocalRunning: Boolean(
            roundStreamControlsRef.current.get(nextRoomId)?.controller,
          ),
        }),
      ));
      if (shouldApplyRoomRefresh(activeRoomIdRef.current, nextRoomId, select)) {
        activeRoomIdRef.current = nextRoomId;
        setActive(nextActive);
        resetMessageNavigation(nextActive);
      }
    }
    setProviders(data.providers || providers);
    setTemplates(data.templates || templates);
    setMemberTemplates(data.member_templates || memberTemplates);
    setCapabilityPacks(data.capability_packs || capabilityPacks);
    setPluginRegistry(Object.hasOwn(data, "plugin_registry") ? data.plugin_registry : null);
    setPluginLifecycle(Object.hasOwn(data, "plugin_lifecycle") ? data.plugin_lifecycle : null);
    return data;
  };

  const loadOlderMessages = async () => {
    const targetRoomId = room?.id || "";
    const cursor = messageHistory.nextCursor;
    if (
      !targetRoomId
      || messageHistory.roomId !== targetRoomId
      || messageHistory.loading
      || !messageHistory.hasMore
      || !cursor
    ) return;
    setMessageHistory((current) => current.roomId === targetRoomId
      ? { ...current, loading: true, error: "" }
      : current);
    try {
      const data = await api.messageHistory(targetRoomId, { before: cursor, limit: 50 });
      setActive((current) => current?.room?.id === targetRoomId
        ? {
          ...current,
          messages: mergeUniqueHistoryRecords(current.messages, data.messages),
          director_decisions: mergeUniqueHistoryRecords(
            current.director_decisions,
            data.director_decisions,
          ),
          message_history: {
            has_more: Boolean(data.has_more && data.next_cursor),
            next_cursor: String(data.next_cursor || ""),
          },
        }
        : current);
      setMessageHistory((current) => (
        current.roomId === targetRoomId && current.nextCursor === cursor
          ? {
            ...current,
            nextCursor: String(data.next_cursor || ""),
            hasMore: Boolean(data.has_more && data.next_cursor),
            loading: false,
            error: "",
          }
          : current
      ));
    } catch (requestError) {
      setMessageHistory((current) => current.roomId === targetRoomId
        ? { ...current, loading: false, error: requestError.message }
        : current);
    }
  };

  const runMessageSearch = async ({ append = false } = {}) => {
    const targetRoomId = room?.id || "";
    const query = String(append ? messageSearch.query : messageSearchInput).trim();
    const cursor = append ? messageSearch.nextCursor : "";
    if (!targetRoomId || !query) {
      setMessageSearchInput("");
      setMessageSearch(emptyMessageSearchState(targetRoomId));
      return;
    }
    if (query.length > 200) {
      setMessageSearch((current) => ({
        ...(append ? current : emptyMessageSearchState(targetRoomId)),
        roomId: targetRoomId,
        query,
        loading: false,
        error: "搜索词最多 200 个字符",
      }));
      return;
    }
    if (append && (messageSearch.loading || !messageSearch.hasMore || !cursor)) return;
    setMessageSearch((current) => ({
      ...(append ? current : emptyMessageSearchState(targetRoomId)),
      roomId: targetRoomId,
      query,
      loading: true,
      error: "",
    }));
    try {
      const data = await api.messageHistory(targetRoomId, {
        before: cursor,
        limit: 30,
        query,
      });
      setMessageSearch((current) => (
        current.roomId === targetRoomId && current.query === query
          ? mergeMessageSearchPage(current, data, { append })
          : current
      ));
    } catch (requestError) {
      setMessageSearch((current) => (
        current.roomId === targetRoomId && current.query === query
          ? { ...current, loading: false, error: requestError.message }
          : current
      ));
    }
  };

  const clearMessageSearch = () => {
    setMessageSearchInput("");
    setMessageSearch(emptyMessageSearchState(room?.id));
  };

  const refreshConvergence = async (roomId = room?.id) => {
    if (!roomId) return null;
    const data = await api.convergence(roomId);
    const acceptanceIncluded = Object.hasOwn(data, "storage_sample_acceptance");
    setActive((current) => current?.room?.id === roomId
      ? {
        ...current,
        convergence: data.convergence,
        ...(acceptanceIncluded
          ? { storage_sample_acceptance: data.storage_sample_acceptance ?? null }
          : {}),
      }
      : current);
    return { convergence: data.convergence, acceptanceIncluded };
  };

  const syncConvergence = (roomId = room?.id) => {
    void refreshConvergence(roomId).catch((requestError) => setError(requestError.message));
  };

  const refreshDecisionPackages = async (roomId) => {
    if (!roomId || !decisionLineageEnabled) return null;
    try {
      const data = await api.room(roomId);
      setActive((current) => current?.room?.id === roomId
        ? {
          ...current,
          decision_packages: data.decision_packages || [],
          paper_portfolios: data.paper_portfolios || current.paper_portfolios || [],
        }
        : current);
      return data.decision_packages || [];
    } catch (requestError) {
      setError(requestError.message || "决策谱系刷新失败");
      return null;
    }
  };

  const runProviderPreflight = async (
    targetRoomId = room?.id,
    targetProviderBlockReason = roundProviderBlockReason,
  ) => {
    if (!targetRoomId) throw new Error("当前没有可检查的房间。");
    if (targetProviderBlockReason) throw new Error(targetProviderBlockReason);
    if (providerOperationRef.current) throw new Error("模型路由正在更新，请稍后再试。");
    providerOperationRef.current = true;
    setProviderRoutingBusy(true);
    setProviderPreflightState({ status: "checking", reason: "" });
    try {
      const data = await api.preflightProviders(targetRoomId, {
        skip_providers: ["openai"],
      });
      const result = normalizeProviderPreflight(data);
      const reason = result.checks
        .filter((check) => check.ready !== true)
        .map((check) => check.error || `${check.id} 未通过`)
        .slice(0, 3)
        .join("；");
      if (activeRoomIdRef.current === targetRoomId) {
        setProviderPreflightState({
          status: result.confirmed && result.ready ? "ready" : "failed",
          reason: reason || (result.confirmed ? "模型本机配置检查未通过。" : "模型配置结果无法确认。"),
        });
      }
      return data;
    } catch (requestError) {
      if (activeRoomIdRef.current === targetRoomId) {
        setProviderPreflightState({ status: "failed", reason: requestError.message });
      }
      throw requestError;
    } finally {
      providerOperationRef.current = false;
      setProviderRoutingBusy(false);
    }
  };

  const routeEnabledMembers = async (providerId) => {
    if (!room) throw new Error("当前没有可更新的房间。");
    if (roundBusy) throw new Error("讨论进行中或正在暂停，不能修改模型路由。");
    if (providerOperationRef.current) throw new Error("模型路由正在更新，请稍后再试。");
    const targetProvider = providers.find(
      (provider) => normalizedProviderId(provider.id) === normalizedProviderId(providerId),
    );
    if (targetProvider?.policy_disabled === true) {
      throw new Error(`${targetProvider.name || providerId} 已被服务端策略禁用。`);
    }
    if (!providerIsAvailable(targetProvider)) {
      throw new Error(`${targetProvider?.name || providerId} 尚未配置。`);
    }
    const targetRoomId = room.id;
    const enabledMembers = members.filter((member) => member.enabled);
    const unchangedMembers = enabledMembers.filter(
      (member) => normalizedProviderId(member.provider) === normalizedProviderId(providerId),
    );
    const membersToUpdate = enabledMembers.filter(
      (member) => normalizedProviderId(member.provider) !== normalizedProviderId(providerId),
    );

    providerOperationRef.current = true;
    setProviderRoutingBusy(true);
    setError("");
    let refreshError = "";
    try {
      const settled = await Promise.allSettled(
        membersToUpdate.map((member) => api.updateMember(targetRoomId, member.id, {
          provider: providerId,
          model: targetProvider.model || "",
          expected_version: member.version,
        })),
      );
      const failed = settled.flatMap((result, index) => result.status === "rejected"
        ? [{
            id: membersToUpdate[index].id,
            name: membersToUpdate[index].name,
            error: result.reason?.message || "请求失败",
          }]
        : []);
      try {
        await refreshRooms(targetRoomId);
      } catch (requestError) {
        refreshError = requestError.message;
      }
      const updated = settled.length - failed.length;
      const summary = {
        total: enabledMembers.length,
        updated,
        unchanged: unchangedMembers.length,
        succeeded: updated + unchangedMembers.length,
        failed,
        refreshError,
      };
      if (failed.length || refreshError) {
        const failureDetails = failed
          .slice(0, 4)
          .map((item) => `${item.name}：${item.error}`)
          .join("；");
        setError([
          `模型路由批量更新完成 ${summary.succeeded}/${summary.total} 位。`,
          failureDetails,
          refreshError ? `刷新失败：${refreshError}` : "",
        ].filter(Boolean).join(" "));
      }
      return summary;
    } finally {
      providerOperationRef.current = false;
      setProviderRoutingBusy(false);
    }
  };

  const changeComposer = (value) => {
    setComposer(value);
    setComposerMentions((current) => current.filter((mention) => value.includes(`@${mention.name}`)));
    if (messageNotice) setMessageNotice("");
  };

  const fillActionDeskComposer = (value) => {
    const candidate = value && typeof value === "object" ? value : {};
    const source = candidate.source && typeof candidate.source === "object" ? candidate.source : {};
    const text = typeof candidate.text === "string" ? candidate.text.trim() : "";
    const sourceReady = (
      typeof source.artifactId === "string"
      && source.artifactId.trim()
      && Number.isSafeInteger(source.artifactVersion)
      && source.artifactVersion > 0
      && typeof source.actionId === "string"
      && source.actionId.trim()
      && /^[0-9a-f]{64}$/.test(String(source.actionSnapshotSha256 || "").toLowerCase())
    );
    if (String(candidate.roomId || "") !== activeRoomId || !text || !sourceReady) {
      setError("行动项精确来源已经变化，请重新读取行动台后再填入讨论框。");
      return;
    }
    const nextComposer = composer.trim() ? `${composer.trim()}\n\n${text}` : text;
    changeComposer(nextComposer);
    setError("");
    const composerTarget = document.querySelector(".composer textarea");
    if (window.matchMedia("(max-width: 1180px)").matches) {
      inspectorPostCloseFocusRef.current = composerTarget;
      setInspectorOpen(false);
    } else {
      requestAnimationFrame(() => composerTarget?.focus());
    }
  };

  const fillRoundFocusObjective = (value) => {
    const candidate = value && typeof value === "object" ? value : {};
    const request = normalizeProjectRoundFocusAuthorization(candidate.request);
    const objective = typeof candidate.objective === "string" ? candidate.objective.trim() : "";
    const roomMatches = String(candidate.roomId || "") === activeRoomId;
    const artifactsMatch = String(candidate.artifactFingerprint || "")
      === roundFocusArtifactFingerprint;
    const roomContextMatches = String(candidate.roomContextFingerprint || "")
      === roundFocusRoomContextFingerprint;
    const pluginMatches = String(candidate.pluginRegistrySnapshotSha256 || "").toLowerCase()
      === String(room?.plugin_registry_snapshot_sha256 || "").toLowerCase();
    if (
      !request.valid
      || !objective
      || !roomMatches
      || !artifactsMatch
      || !roomContextMatches
      || !pluginMatches
      || roundBusy
      || roundAvailability.hasPendingRound
      || roundLaunchOpen
    ) {
      setError("下一轮焦点已经变化或当前轮次未结束，请重新读取后再填入。");
      return;
    }
    setComposer(objective);
    setComposerMentions([]);
    setRoundFocusAuthorization({
      roomId: activeRoomId,
      artifactFingerprint: roundFocusArtifactFingerprint,
      roomContextFingerprint: roundFocusRoomContextFingerprint,
      pluginRegistrySnapshotSha256: String(room?.plugin_registry_snapshot_sha256 || "").toLowerCase(),
      request: candidate.request,
    });
    setError("");
    if (messageNotice) setMessageNotice("");
    const composerTarget = document.querySelector(".composer textarea");
    if (window.matchMedia("(max-width: 1180px)").matches) {
      inspectorPostCloseFocusRef.current = composerTarget;
      setInspectorOpen(false);
    } else {
      requestAnimationFrame(() => composerTarget?.focus());
    }
  };

  const rememberComposerMention = (member) => {
    setComposerMentions((current) => current.some((mention) => mention.member_id === member.id)
      ? current
      : [...current, {
          member_id: member.id,
          name: member.name,
          expected_member_version: Number(member.version) || 1,
        }]
    );
  };

  const handleMessageStreamEvent = (targetRoomId, event) => {
    if (event.type === "user_message") {
      updateActiveRoom(targetRoomId, (current) => ({
        ...current,
        messages: appendUniqueMessage(current.messages, event.message),
      }));
    } else if (event.type === "message_stored") {
      setRuntimeField(targetRoomId, "messageNotice", event.notice || "消息已保存；当前房间不会自动响应未点名消息。");
    } else if (event.type === "chat_request_resumed") {
      setRuntimeField(targetRoomId, "messageNotice", "正在恢复上次中断的定向回复请求。 ");
    } else if (event.type === "interjection_queued") {
      setRuntimeField(targetRoomId, "messageNotice", event.target_member_ids?.length
        ? "插话已加入当前轮次；被点名成员将在流程允许的下一调度节点回应。"
        : "插话已加入当前轮次；主持人将在下一安全调度节点重新安排发言。"
      );
    } else if (event.type === "mention_queued") {
      setRuntimeField(targetRoomId, "messageNotice", event.message || "定向回复请求已保留，请稍后刷新查看。 ");
    } else if (event.type === "speaker_started") {
      updateRuntime(targetRoomId, (runtime) => reduceRoomRuntimeEvent(runtime, event));
    } else if (event.type === "message") {
      updateActiveRoom(targetRoomId, (current) => ({
        ...current,
        messages: appendUniqueMessage(current.messages, event.message),
      }));
      updateRuntime(targetRoomId, (runtime) => reduceRoomRuntimeEvent(runtime, event));
    } else if (event.type === "speaker_failed") {
      if (event.message) {
        updateActiveRoom(targetRoomId, (current) => ({
          ...current,
          messages: appendUniqueMessage(current.messages, event.message),
        }));
      }
      updateRuntime(targetRoomId, (runtime) => reduceRoomRuntimeEvent(runtime, event));
    } else if (event.type === "chat_request_completed") {
      updateActiveRoom(targetRoomId, (current) => ({
        ...current,
        pending_chat_requests: (current.pending_chat_requests || [])
          .filter((request) => request.id !== event.request_id),
      }));
      setRuntimeField(targetRoomId, "messageNotice", event.failures
        ? `定向回复完成：${event.completed} 位成功，${event.failures} 位失败。`
        : `已由 ${event.completed} 位被点名成员依次回应。`
      );
    } else if (event.type === "chat_request_deferred") {
      setRuntimeField(targetRoomId, "messageNotice", "定向回复仍由另一处理器持有；租约释放或服务恢复后可继续。 ");
    } else if (event.type === "error") {
      throw new Error(event.error || "群聊消息处理失败");
    }
  };

  const sendMessage = async () => {
    const content = composer.trim();
    if (!content || !room || messageSending || roundState.pausing || (roundState.running && !roundState.roundId)) return;
    const targetRoomId = String(room.id);
    const selectedMentions = composerMentions;
    const { mentions, ambiguousNames } = resolveComposerMentions(
      content,
      members,
      selectedMentions,
    );
    if (ambiguousNames.length) {
      setRuntimeField(
        targetRoomId,
        "streamError",
        `同名成员“${ambiguousNames.join("、")}”无法仅凭手输 @ 区分。请从 @ 菜单选择具体成员后再发送。`,
      );
      return;
    }
    const expectedRoundId = roundState.running ? roundState.roundId : "";
    const previousPending = pendingMessagesRef.current.get(targetRoomId);
    const samePending = previousPending
      && previousPending.content === content
      && previousPending.expected_round_id === expectedRoundId;
    const clientMessageId = samePending
      ? previousPending.client_message_id
      : `web-${globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`}`;
    const requestPayload = {
      content,
      mentions,
      expected_round_id: expectedRoundId,
      client_message_id: clientMessageId,
    };
    pendingMessagesRef.current.set(targetRoomId, { ...requestPayload });
    setComposer("");
    setComposerMentions([]);
    setRuntimeField(targetRoomId, "messageNotice", "");
    setRuntimeField(targetRoomId, "streamError", "");
    setRuntimeField(targetRoomId, "messageSending", true);
    try {
      await streamMessage(
        targetRoomId,
        requestPayload,
        (event) => handleMessageStreamEvent(targetRoomId, event),
      );
      if (pendingMessagesRef.current.get(targetRoomId)?.client_message_id === clientMessageId) {
        pendingMessagesRef.current.delete(targetRoomId);
      }
    } catch (requestError) {
      if (activeRoomIdRef.current === targetRoomId) {
        setComposer(content);
        setComposerMentions((current) => current.length ? current : selectedMentions);
      }
      setRuntimeField(targetRoomId, "streamError", requestError.message);
    } finally {
      setRuntimeField(targetRoomId, "messageSending", false);
    }
  };

  const resumePendingChatRequest = async (requestId) => {
    if (!room || !requestId || messageSending) return;
    const targetRoomId = String(room.id);
    setRuntimeField(targetRoomId, "streamError", "");
    setRuntimeField(targetRoomId, "messageSending", true);
    try {
      await streamResumeChatRequest(
        targetRoomId,
        requestId,
        (event) => handleMessageStreamEvent(targetRoomId, event),
      );
    } catch (requestError) {
      setRuntimeField(targetRoomId, "streamError", requestError.message || "定向回复恢复失败");
    } finally {
      setRuntimeField(targetRoomId, "messageSending", false);
    }
  };

  const handleRoundEvent = (targetRoomId, event) => {
    const control = streamControlFor(targetRoomId);
    const traceEventRoundId = String(
      event.round?.id || event.round_id || event.message?.round_id || control.roundId || "",
    );
    if (traceEventRoundId && ROUND_TRACE_STALE_EVENT_TYPES.has(event.type)) {
      setRoundExecutionTrace((current) => (
        current.roomId === targetRoomId
        && current.roundId === traceEventRoundId
        && current.trace
          ? { ...current, stale: true }
          : current
      ));
      setDiscussionAudit((current) => (
        current.roomId === targetRoomId
        && current.roundId === traceEventRoundId
        && current.audit
          ? { ...current, stale: true }
          : current
      ));
    }
    if (event.type === "round_started" || event.type === "round_resumed") {
      control.roundId = String(event.round?.id || "");
      updateActiveRoom(targetRoomId, (current) => ({
        ...current,
        latest_round: event.round,
        pending_round: null,
        pending_round_checkpoint: null,
        messages: event.user_message
          ? appendUniqueMessage(current.messages, event.user_message)
          : current.messages,
      }));
      updateRuntime(targetRoomId, (runtime) => reduceRoomRuntimeEvent(runtime, event));
    } else if (event.type === "market_snapshot") {
      if (activeRoomIdRef.current === targetRoomId) setMarketSnapshot(event.snapshot);
      if (event.message) {
        updateActiveRoom(targetRoomId, (current) => ({
          ...current,
          messages: appendUniqueMessage(current.messages, event.message),
        }));
      }
    } else if (event.type === "director_decision") {
      const decision = normalizeDirectorDecision(event, targetRoomId, control.roundId);
      if (decision.room_id !== targetRoomId) return;
      updateRuntime(
        targetRoomId,
        (runtime) => reduceRoomRuntimeEvent(runtime, event, decision),
      );
      updateActiveRoom(targetRoomId, (current) => ({
        ...current,
        director_decisions: mergeDirectorDecisions(current.director_decisions, decision),
        convergence: event.convergence || current.convergence,
      }));
    } else if (event.type === "convergence_updated") {
      updateActiveRoom(targetRoomId, (current) => ({
        ...current,
        convergence: event.convergence,
      }));
    } else if (event.type === "speaker_started") {
      updateRuntime(targetRoomId, (runtime) => reduceRoomRuntimeEvent(runtime, event));
    } else if (event.type === "message") {
      updateActiveRoom(targetRoomId, (current) => ({
        ...current,
        messages: appendUniqueMessage(current.messages, event.message),
      }));
      updateRuntime(targetRoomId, (runtime) => reduceRoomRuntimeEvent(runtime, event));
    } else if (event.type === "speaker_failed") {
      if (event.message) {
        updateActiveRoom(targetRoomId, (current) => ({
          ...current,
          messages: appendUniqueMessage(current.messages, event.message),
        }));
      }
      updateRuntime(targetRoomId, (runtime) => {
        const next = reduceRoomRuntimeEvent(runtime, event);
        if (event.message) return next;
        return {
          ...next,
          transientErrors: [...next.transientErrors, {
            id: `${event.member.id}-${Date.now()}`,
            name: event.member.name,
            message: event.error,
          }],
        };
      });
    } else if (event.type === "speaker_skipped") {
      updateRuntime(targetRoomId, (runtime) => reduceRoomRuntimeEvent(runtime, event));
    } else if (event.type === "observation_proposed") {
      updateActiveRoom(targetRoomId, (current) => ({
        ...current,
        observations: (current.observations || []).some((item) => item.id === event.observation.id)
          ? current.observations
          : [event.observation, ...(current.observations || [])],
      }));
    } else if (event.type === "observation_proposal_rejected") {
      setRuntimeField(targetRoomId, "transientErrors", (current) => [...current, {
        id: `observation-${Date.now()}`,
        name: event.member?.name || "投委会",
        message: `观察提案未进入待确认队列：${event.error}`,
      }]);
    } else if (event.type === "round_paused") {
      control.terminalSeen = true;
      control.pauseRequested = false;
      control.roundId = "";
      updateActiveRoom(targetRoomId, (current) => {
        const pausedRound = event.round || (current.pending_round
          ? { ...current.pending_round, status: "PAUSED", pause_requested: false }
          : null);
        return {
          ...current,
          latest_round: pausedRound || current.latest_round,
          pending_round: pausedRound,
          pending_round_checkpoint: event.checkpoint || current.pending_round_checkpoint,
        };
      });
      updateRuntime(targetRoomId, (runtime) => ({
        ...reduceRoomRuntimeEvent(runtime, event),
        messageNotice: "已暂停在安全检查点。已完成发言会保留，继续时从下一位开始。",
      }));
    } else if (event.type === "round_completed") {
      const pauseWasRequested = control.pauseRequested;
      control.terminalSeen = true;
      control.pauseRequested = false;
      control.roundId = "";
      updateActiveRoom(targetRoomId, (current) => {
        const completedRound = current.latest_round
          ? { ...current.latest_round, status: event.status }
          : current.latest_round;
        const remainsPaused = String(event.status || "").toUpperCase() === "PAUSED";
        return {
          ...current,
          latest_round: completedRound,
          pending_round: remainsPaused
            ? (current.pending_round ? { ...current.pending_round, status: "PAUSED" } : completedRound)
            : null,
          pending_round_checkpoint: remainsPaused ? current.pending_round_checkpoint : null,
          convergence: event.convergence || current.convergence,
        };
      });
      updateRuntime(targetRoomId, (runtime) => {
        const next = reduceRoomRuntimeEvent(runtime, event);
        const messageNotice = event.pending_interjections
          ? "轮次在结束竞态中收到新插话，已安全暂停；恢复后会继续处理。 "
          : pauseWasRequested && String(event.status || "").toUpperCase() !== "PAUSED"
            ? "本轮已在暂停生效前完成，无需恢复。"
            : next.messageNotice;
        return { ...next, messageNotice };
      });
      void refreshConvergence(targetRoomId).catch((requestError) => {
        setRuntimeField(targetRoomId, "streamError", requestError.message);
      });
    } else if (event.type === "error") {
      throw new Error(event.error || "讨论轮次失败");
    }
  };

  const consumeRoundStream = async (targetRoomId, streamer) => {
    const control = streamControlFor(targetRoomId);
    if (control.controller) return;
    const controller = new AbortController();
    let streamError = null;
    let streamStarted = false;
    let authoritativeStateKnown = false;
    let authoritativeRoundRunning = false;
    let authoritativeRoundId = "";
    let authoritativePendingRoundId = "";
    control.controller = controller;
    try {
      await streamer(
        (event) => {
          if (event.type === "round_started" || event.type === "round_resumed") {
            streamStarted = true;
          }
          handleRoundEvent(targetRoomId, event);
        },
        controller.signal,
      );
    } catch (requestError) {
      if (requestError.name !== "AbortError") {
        streamError = requestError;
        setRuntimeField(targetRoomId, "streamError", requestError.message);
      }
    } finally {
      try {
        const data = await refreshRooms(targetRoomId);
        const authoritativeRound = data.active?.latest_round || null;
        const stillRunning = String(authoritativeRound?.status || "").toUpperCase() === "RUNNING";
        authoritativeStateKnown = true;
        authoritativeRoundRunning = stillRunning;
        authoritativeRoundId = String(authoritativeRound?.id || "");
        authoritativePendingRoundId = String(data.active?.pending_round?.id || "");
        if (control.terminalSeen) {
          updateRuntime(targetRoomId, (runtime) => reduceRoomRuntimeEvent(
            runtime,
            { type: "round_completed" },
          ));
        } else if (stillRunning) {
          control.roundId = String(authoritativeRound.id || control.roundId || "");
          setRoundStateForRoom(targetRoomId, (current) => ({
            ...current,
            running: true,
            pausing: control.pauseRequested || Boolean(authoritativeRound.pause_requested),
            roundId: control.roundId,
          }));
          if (control.pauseRequested) {
            setRuntimeField(
              targetRoomId,
              "streamError",
              "暂停请求已发出，但服务端仍在完成当前成员发言。为避免重复轮次，讨论保持锁定。",
            );
          }
        } else {
          updateRuntime(targetRoomId, (runtime) => reduceRoomRuntimeEvent(
            runtime,
            { type: "round_completed" },
          ));
          control.pauseRequested = false;
          control.roundId = "";
        }
      } catch (refreshError) {
        if (control.pauseRequested) {
          setRuntimeField(
            targetRoomId,
            "streamError",
            "暂停请求已发出，但暂时无法确认服务端状态。为避免重复轮次，开始新一轮仍保持锁定。",
          );
          setRoundStateForRoom(targetRoomId, (current) => ({
            ...current,
            running: true,
            pausing: true,
          }));
        } else if (!control.terminalSeen) {
          setRuntimeField(targetRoomId, "streamError", refreshError.message);
        }
      } finally {
        if (control.controller === controller) control.controller = null;
      }
    }
    return {
      error: streamError,
      started: streamStarted,
      authoritativeRoundId,
      authoritativePendingRoundId,
      safeToRetry: Boolean(
        streamError
        && !streamStarted
        && authoritativeStateKnown
        && !authoritativeRoundRunning
      ),
    };
  };

  const requestRoundLaunchPlan = async (context) => {
    const targetRoomId = context.roomId;
    const streamControl = streamControlFor(targetRoomId);
    if (streamControl.controller || (streamControl.transition && streamControl.transition !== "plan")) {
      throw new Error("讨论状态已变化，无法读取新的启动确认单。");
    }

    const previousControl = roundLaunchControlRef.current;
    previousControl.controller?.abort();
    const sequence = previousControl.sequence + 1;
    const controller = new AbortController();
    streamControl.transition = "plan";
    roundLaunchControlRef.current = {
      sequence,
      controller,
      context,
      plan: null,
      phase: "loading",
    };
    setRoundLaunch({
      status: "loading",
      ...context,
      plan: null,
      error: "",
    });

    try {
      const data = await api.roundLaunchPlan(
        targetRoomId,
        {
          objective: context.objective,
          skip_providers: [...ROUND_LAUNCH_SKIP_PROVIDERS],
          ...(context.roundContextAuthorizations
            ? { round_context_authorizations: context.roundContextAuthorizations }
            : {}),
        },
        controller.signal,
      );
      if (roundLaunchControlRef.current.sequence !== sequence) return;
      if (activeRoomIdRef.current !== targetRoomId) {
        throw new Error("房间已切换，原启动确认单已失效。");
      }
      const contextState = roundLaunchPlanContextState(data.plan, {
        roomId: targetRoomId,
        roomSettingsVersion: context.roomSettingsVersion,
        objective: context.objective,
        skipProviders: ROUND_LAUNCH_SKIP_PROVIDERS,
        roundContextAuthorizations: context.roundContextAuthorizations,
      });
      if (!contextState.matches) throw new Error(contextState.error);
      roundLaunchControlRef.current = {
        sequence,
        controller: null,
        context,
        plan: contextState.plan,
        phase: "ready",
      };
      setRoundLaunch({
        status: "ready",
        ...context,
        plan: contextState.plan,
        error: "",
      });
    } catch (requestError) {
      if (requestError?.name === "AbortError" || roundLaunchControlRef.current.sequence !== sequence) return;
      roundLaunchControlRef.current = {
        sequence,
        controller: null,
        context,
        plan: null,
        phase: "error",
      };
      setRoundLaunch({
        status: "error",
        ...context,
        plan: null,
        error: requestError.message || "启动确认单读取失败。",
      });
    } finally {
      if (roundLaunchControlRef.current.controller === controller) {
        roundLaunchControlRef.current.controller = null;
      }
    }
  };

  const startChatGPTCollaboration = (launchTrigger) => {
    if (!room || roundBusy || chatGPTCollaborationOpen) return;
    const activeTrigger = launchTrigger || document.activeElement;
    chatGPTCollaborationRestoreFocusRef.current = (
      activeTrigger
      && activeTrigger !== document.body
      && typeof activeTrigger.focus === "function"
    ) ? activeTrigger : null;
    setError("");
    setChatGPTCollaborationOpen(true);
  };

  const startRound = async (launchTrigger) => {
    if (!room || roundBusy || providerRoutingBusy || roundCancelBusy) return;
    const targetRoomId = String(room.id);
    const control = streamControlFor(targetRoomId);
    if (control.controller || control.transition || roundLaunchControlRef.current.context) return;
    if (roundAvailability.hasPendingRound) {
      setError(roundAvailability.blockReason);
      return;
    }
    if (!canAttemptNewRound) {
      setError(newRoundBlockReason);
      return;
    }
    const activeLaunchTrigger = launchTrigger || document.activeElement;
    roundLaunchRestoreFocusRef.current = (
      activeLaunchTrigger
      && activeLaunchTrigger !== document.body
      && typeof activeLaunchTrigger.focus === "function"
    ) ? activeLaunchTrigger : null;
    try {
      const requestContext = createRoundLaunchRequestContext({
        roomId: targetRoomId,
        roomSettingsVersion: Number(room.settings_version),
        objective: composer.trim() || room.objective,
        roundContextAuthorizations: activeRoundContextAuthorizations,
      }, {
        createRequestId: newClientRoundRequestId,
      });
      const context = {
        ...requestContext,
        composerDraft: composer,
        composerMentions: composerMentions.map((mention) => ({ ...mention })),
        roundFocusAuthorizationSnapshot: roundFocusRequired ? roundFocusAuthorization : null,
        baselineRoundId: String(latestRound?.id || ""),
      };
      setError("");
      setRuntimeField(targetRoomId, "streamError", "");
      await requestRoundLaunchPlan(context);
    } catch (requestError) {
      setError(requestError.message || "无法创建启动确认单。");
    }
  };

  const retryRoundLaunchPlan = async () => {
    const previous = roundLaunchControlRef.current.context;
    if (!previous || roundLaunchControlRef.current.controller || !room) return;
    if (activeRoomIdRef.current !== previous.roomId) {
      discardRoundLaunch();
      setError("房间已切换，请在当前房间重新发起讨论。");
      return;
    }
    try {
      const requestContext = createRoundLaunchRequestContext({
        roomId: previous.roomId,
        roomSettingsVersion: Number(room.settings_version),
        objective: previous.objective,
        roundContextAuthorizations: activeRoundContextAuthorizations,
      }, {
        previous,
        createRequestId: newClientRoundRequestId,
      });
      const context = {
        ...requestContext,
        composerDraft: previous.composerDraft || "",
        composerMentions: Array.isArray(previous.composerMentions)
          ? previous.composerMentions.map((mention) => ({ ...mention }))
          : [],
        roundFocusAuthorizationSnapshot: roundFocusRequired ? roundFocusAuthorization : null,
        baselineRoundId: previous.baselineRoundId || "",
      };
      await requestRoundLaunchPlan(context);
    } catch (requestError) {
      setRoundLaunch((current) => ({
        ...current,
        status: "error",
        plan: null,
        error: requestError.message || "启动确认单重试失败。",
      }));
    }
  };

  const confirmRoundLaunch = async (payload) => {
    const launchControl = roundLaunchControlRef.current;
    const context = launchControl.context;
    const plan = launchControl.plan;
    if (!context || !plan || launchControl.phase !== "ready") {
      throw new Error("启动确认单已失效，请重新读取。");
    }
    const contextState = roundLaunchPlanContextState(plan, {
      roomId: activeRoomIdRef.current,
      roomSettingsVersion: Number(room?.settings_version),
      objective: context.objective,
      skipProviders: ROUND_LAUNCH_SKIP_PROVIDERS,
      roundContextAuthorizations: activeRoundContextAuthorizations,
    });
    const skipMatches = Array.isArray(payload?.skip_providers)
      && payload.skip_providers.length === ROUND_LAUNCH_SKIP_PROVIDERS.length
      && payload.skip_providers.every((item, index) => item === ROUND_LAUNCH_SKIP_PROVIDERS[index]);
    const payloadMatches = payload?.client_round_request_id === context.clientRoundRequestId
      && payload?.plan_hash === plan.plan_hash
      && payload?.objective === context.objective
      && Number.isInteger(payload?.max_provider_calls)
      && payload.max_provider_calls >= PROVIDER_CALL_LIMIT_MIN
      && payload.max_provider_calls <= PROVIDER_CALL_LIMIT_MAX
      && JSON.stringify(payload?.round_context_authorizations || null)
        === JSON.stringify(context.roundContextAuthorizations || null)
      && skipMatches;
    if (!contextState.matches || !payloadMatches) {
      const message = contextState.error || "启动确认内容已变化，请重新读取确认单。";
      roundLaunchControlRef.current = {
        ...launchControl,
        controller: null,
        plan: null,
        phase: "error",
      };
      setRoundLaunch((current) => ({ ...current, status: "error", plan: null, error: message }));
      throw new Error(message);
    }

    const targetRoomId = context.roomId;
    const control = streamControlFor(targetRoomId);
    if (control.controller || control.transition !== "plan") {
      throw new Error("讨论状态已变化，本次确认未启动新轮次。");
    }
    roundLaunchControlRef.current = { ...launchControl, phase: "starting" };
    setRoundLaunch((current) => ({ ...current, status: "starting", error: "" }));
    control.roundId = "";
    control.pauseRequested = false;
    control.terminalSeen = false;
    updateRuntime(targetRoomId, (runtime) => ({
      ...runtime,
      roundState: emptyRoundState({
        running: true,
        memberStatus: Object.fromEntries(plan.members.map((member) => [member.id, "queued"])),
      }),
      typingMember: null,
      transientErrors: [],
      messageNotice: "",
      streamError: "",
    }));
    if (activeRoomIdRef.current === targetRoomId) {
      setComposer("");
      setComposerMentions([]);
      setRoundFocusAuthorization(null);
      setFootballRoundContextAuthorization(null);
      setStockRoundContextAuthorization(null);
    }
    pendingMessagesRef.current.delete(targetRoomId);
    const streamPromise = consumeRoundStream(
      targetRoomId,
      (onEvent, signal) => streamRound(targetRoomId, payload, onEvent, signal),
    );
    control.transition = "";
    roundLaunchControlRef.current = {
      sequence: launchControl.sequence,
      controller: null,
      context: null,
      plan: null,
      phase: "idle",
    };
    if (activeRoomIdRef.current === targetRoomId) {
      roundLaunchRestoreFocusRef.current = roundLaunchSuccessFocusRef.current;
    }
    setRoundLaunch(emptyRoundLaunchState());
    const streamResult = await streamPromise;
    const noNewAuthoritativeRound = !streamResult?.authoritativePendingRoundId
      && String(streamResult?.authoritativeRoundId || "") === String(context.baselineRoundId || "");
    if (
      streamResult?.safeToRetry
      && noNewAuthoritativeRound
      && activeRoomIdRef.current === targetRoomId
    ) {
      control.transition = "plan";
      roundLaunchControlRef.current = {
        sequence: launchControl.sequence,
        controller: null,
        context,
        plan: null,
        phase: "error",
      };
      setComposer(context.composerDraft || "");
      setComposerMentions(Array.isArray(context.composerMentions) ? context.composerMentions : []);
      setRoundFocusAuthorization(context.roundFocusAuthorizationSnapshot || null);
      setRoundLaunch({
        status: "error",
        ...context,
        plan: null,
        error: `${streamResult.error.message || "讨论未启动"}；可重新读取确认单后重试。`,
      });
    }
  };

  const resumePausedRound = async () => {
    if (!room || roundBusy || providerRoutingBusy || roundCancelBusy || !roundAvailability.canResume || !pendingRound || !pendingRoundCheckpoint) return;
    const targetRoomId = String(room.id);
    const targetRoundId = String(pendingRound.id);
    const control = streamControlFor(targetRoomId);
    if (control.controller || control.transition) return;
    const enabledMembers = members.filter((member) => member.enabled);
    const spoken = new Set(pendingRoundCheckpoint.spoken_member_ids || []);
    control.transition = "resume";
    setRuntimeField(targetRoomId, "streamError", "");
    try {
      control.roundId = targetRoundId;
      control.pauseRequested = false;
      control.terminalSeen = false;
      updateRuntime(targetRoomId, (runtime) => ({
        ...runtime,
        roundState: emptyRoundState({
          running: true,
          memberStatus: Object.fromEntries(enabledMembers.map((member) => [
            member.id,
            spoken.has(member.id) ? "done" : "queued",
          ])),
          roundId: targetRoundId,
        }),
        typingMember: null,
        transientErrors: [],
        streamError: "",
      }));
      const streamPromise = consumeRoundStream(
        targetRoomId,
        (onEvent, signal) => streamResumeRound(targetRoomId, targetRoundId, onEvent, signal),
      );
      control.transition = "";
      await streamPromise;
    } finally {
      if (control.transition === "resume") control.transition = "";
    }
  };

  const refreshMarket = async () => {
    if (!storageRoomId || marketLoading) return;
    setMarketLoading(true);
    try {
      const [snapshotResult, statusResult] = await Promise.all([
        api.storageSnapshot(true),
        api.storageStatus(),
      ]);
      setMarketSnapshot(snapshotResult.snapshot);
      setMarketStatus(statusResult.status);
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      setMarketLoading(false);
    }
  };

  const refreshMarketReadiness = (options = {}) => {
    const targetRoomId = String(options?.roomId || storageRoomId || "");
    if (!targetRoomId || storageRoomIdRef.current !== targetRoomId) {
      return Promise.resolve({ status: "skipped", started: false, applied: false });
    }
    return marketReadinessRequestRef.current.run({
      forceRequest: options?.forceRequest === true,
      request: (signal) => Promise.all([
        api.storageReadiness(true, signal, targetRoomId),
        api.storageStatus(signal),
      ]),
      onSuccess: ([readinessResult, statusResult]) => {
        if (storageRoomIdRef.current !== targetRoomId) return;
        setMarketReadiness(readinessResult.readiness);
        setMarketStatus(statusResult.status);
      },
      onError: (requestError) => {
        if (storageRoomIdRef.current === targetRoomId) setError(requestError.message);
      },
      onLoadingChange: setMarketReadinessLoading,
    });
  };

  const applyMaterial = (material, _wasUpdate = false, closeDialog = true, targetRoomId = activeRoomIdRef.current) => {
    const expectedRoomId = String(targetRoomId || "");
    if (!expectedRoomId || activeRoomIdRef.current !== expectedRoomId) return material;
    materialVersionsRequestRef.current += 1;
    setActive((current) => applyMaterialToRoomSnapshot(current, expectedRoomId, material));
    if (closeDialog) {
      setEditingMaterial(null);
      setMaterialVersions([]);
    }
    return material;
  };

  const applyOfficialAttestation = (attestation, targetRoomId = activeRoomIdRef.current) => {
    if (!attestation?.id) return;
    const expectedRoomId = String(targetRoomId || "");
    if (!expectedRoomId || activeRoomIdRef.current !== expectedRoomId) return;
    setActive((current) => applyOfficialAttestationToRoomSnapshot(
      current,
      expectedRoomId,
      attestation,
    ));
  };

  const openOfficialSupplement = (candidate) => {
    try {
      setMaterialVersions([]);
      setMaterialVersionsLoading(false);
      setEditingMaterial(buildOfficialSupplementDraft(candidate));
    } catch (requestError) {
      setError(requestError.message);
    }
  };

  const saveMaterial = async (form) => {
    const targetRoomId = String(room?.id || "");
    try {
      const data = form.id
        ? await api.updateMaterial(targetRoomId, form.id, { ...form, expected_version: form.version })
        : await api.addMaterial(targetRoomId, form);
      const material = applyMaterial(data.material, Boolean(form.id), true, targetRoomId);
      syncConvergence(targetRoomId);
      return material;
    } catch (requestError) {
      if (activeRoomIdRef.current === targetRoomId) setError(requestError.message);
      throw requestError;
    }
  };

  const openMaterial = async (material) => {
    const requestId = materialVersionsRequestRef.current + 1;
    materialVersionsRequestRef.current = requestId;
    const stagedAttestation = officialAttestations.find((attestation) => (
      attestation.material_id === material.id
      && Number(attestation.material_version) === Number(material.version)
      && attestation.state === "staged"
      && attestation.integrity_ready === true
    ));
    setEditingMaterial(stagedAttestation ? {
      ...material,
      official_attestation: stagedAttestation,
      official_supplement_v1: {
        version: "official_supplement_v1",
        symbol: stagedAttestation.symbol,
        official_url: stagedAttestation.official_url,
        fiscal_period: stagedAttestation.fiscal_period,
        material_kind: stagedAttestation.material_kind,
        original_error_codes: stagedAttestation.original_error_codes || [],
        user_confirmed: true,
      },
    } : material);
    setMaterialVersions([]);
    setMaterialVersionsLoading(true);
    try {
      const data = await api.materialVersions(room.id, material.id);
      if (materialVersionsRequestRef.current !== requestId) return;
      setMaterialVersions(data.versions || []);
    } catch (requestError) {
      if (materialVersionsRequestRef.current !== requestId) return;
      setError(requestError.message);
    } finally {
      if (materialVersionsRequestRef.current === requestId) setMaterialVersionsLoading(false);
    }
  };

  const fetchMaterialUrl = async (form) => {
    const targetRoomId = String(room?.id || "");
    try {
      const data = await api.fetchMaterialUrl(targetRoomId, {
        material_id: form.id || "",
        ...(form.id ? { expected_version: form.version } : {}),
        title: form.title || "",
        url: form.source_url || "",
        metadata: form.metadata || {},
      });
      const material = applyMaterial(data.material, Boolean(form.id), true, targetRoomId);
      syncConvergence(targetRoomId);
      return material;
    } catch (requestError) {
      if (activeRoomIdRef.current === targetRoomId) setError(requestError.message);
      throw requestError;
    }
  };

  const importMaterialFile = async (form, file) => {
    const targetRoomId = String(room?.id || "");
    try {
      const officialSupplement = form.official_supplement_v1;
      const payload = {
        ...await fileToMaterialPayload(file, form.title || "", form.id || "", form.metadata || {}),
        ...(form.id ? { expected_version: form.version } : {}),
        ...(officialSupplement ? { official_supplement: officialSupplement } : {}),
      };
      const data = await api.importMaterialFile(targetRoomId, payload);
      const material = applyMaterial(
        data.material,
        Boolean(form.id),
        !officialSupplement,
        targetRoomId,
      );
      if (officialSupplement) {
        applyOfficialAttestation(
          data.official_attestation || data.material?._official_attestation,
          targetRoomId,
        );
      }
      syncConvergence(targetRoomId);
      return officialSupplement ? data : material;
    } catch (requestError) {
      if (activeRoomIdRef.current === targetRoomId) setError(requestError.message);
      throw requestError;
    }
  };

  const confirmOfficialAttestation = async (materialId, payload) => {
    const targetRoomId = String(room?.id || "");
    try {
      const data = await api.confirmOfficialAttestation(targetRoomId, materialId, payload);
      const confirmedState = confirmedOfficialAttestationDialogState(data);
      applyMaterial(data.material, true, false, targetRoomId);
      applyOfficialAttestation(confirmedState.officialAttestation, targetRoomId);
      syncConvergence(targetRoomId);
      if (activeRoomIdRef.current === targetRoomId) {
        void refreshMarketReadiness({ forceRequest: true, roomId: targetRoomId });
      }
      return data;
    } catch (requestError) {
      if (activeRoomIdRef.current === targetRoomId) setError(requestError.message);
      throw requestError;
    }
  };

  const freezeOfficialEvidence = async (evidence) => {
    const targetRoomId = String(room?.id || "");
    try {
      const data = await api.freezeOfficialEvidence(targetRoomId, evidence);
      applyMaterial(data.material, false, true, targetRoomId);
      syncConvergence(targetRoomId);
      return data;
    } catch (requestError) {
      if (activeRoomIdRef.current === targetRoomId) setError(requestError.message);
      throw requestError;
    }
  };

  const createObservation = async (form) => {
    try {
      const data = await api.createObservation(room.id, form);
      setActive((current) => ({
        ...current,
        observations: [data.observation, ...(current.observations || [])],
      }));
      setObservationOpen(false);
      syncConvergence(room.id);
      if (form.user_decision_id) await refreshDecisionPackages(room.id);
      setObservationLineageSource(null);
      return data.observation;
    } catch (requestError) {
      setError(requestError.message);
      throw requestError;
    }
  };

  const replaceObservation = (observation, scorecard) => {
    setActive((current) => ({
      ...current,
      observations: (current.observations || []).map((item) => item.id === observation.id ? observation : item),
      observation_scorecard: scorecard || current.observation_scorecard || {},
    }));
  };

  const confirmObservation = async (observation) => {
    if (observationLoading) return;
    setObservationLoading(true);
    try {
      const data = await api.confirmObservation(room.id, observation.id);
      replaceObservation(data.observation, data.scorecard);
      syncConvergence(room.id);
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      setObservationLoading(false);
    }
  };

  const bindObservationDecisionLineage = async (observation, lineage) => {
    if (observationLoading) return null;
    setObservationLoading(true);
    try {
      const data = await api.bindObservationDecisionLineage(room.id, observation.id, lineage);
      replaceObservation(data.observation, data.scorecard);
      await refreshDecisionPackages(room.id);
      syncConvergence(room.id);
      return data.observation;
    } catch (requestError) {
      setError(requestError.message);
      throw requestError;
    } finally {
      setObservationLoading(false);
    }
  };

  const reconcileObservations = async () => {
    if (observationLoading) return;
    setObservationLoading(true);
    try {
      const data = await api.reconcileObservations(room.id);
      setActive((current) => ({
        ...current,
        observations: data.observations || [],
        reflections: data.reflections || [],
        observation_scorecard: data.scorecard || {},
      }));
      syncConvergence(room.id);
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      setObservationLoading(false);
    }
  };

  const replaceReflection = (reflection) => {
    setActive((current) => ({
      ...current,
      reflections: (current.reflections || []).some((item) => item.id === reflection.id)
        ? (current.reflections || []).map((item) => item.id === reflection.id ? reflection : item)
        : [reflection, ...(current.reflections || [])],
    }));
  };

  const replacePaperPortfolio = (portfolio) => {
    setActive((current) => {
      const existing = current.paper_portfolios || [];
      return {
        ...current,
        paper_portfolios: existing.some((item) => item.id === portfolio.id)
          ? existing.map((item) => item.id === portfolio.id ? portfolio : item)
          : [portfolio, ...existing],
      };
    });
  };

  const openPaperPortfolio = (portfolio = {}, launchTrigger = null) => {
    paperPortfolioRestoreFocusRef.current = launchTrigger || document.activeElement;
    setEditingPaperPortfolio(portfolio);
  };

  const openPaperPortfolioFromDecision = (decisionPackage, launchTrigger = null) => {
    const anchor = decisionPackage?.anchor || {};
    const selectedOption = anchor.selected_option || {};
    paperPortfolioRestoreFocusRef.current = launchTrigger || document.activeElement;
    setEditingPaperPortfolio({
      lineage_source: {
        package_id: String(decisionPackage?.package_id || anchor.user_decision_id || ""),
        package_state: String(decisionPackage?.state || ""),
        package_integrity_ok: decisionPackage?.integrity_ok === true && anchor.integrity_ok === true,
        user_decision_id: String(anchor.user_decision_id || ""),
        artifact_id: String(anchor.artifact_id || ""),
        artifact_version: Number(anchor.artifact_version) || 0,
        action: String(anchor.action || ""),
        decision_version: String(anchor.decision_version || ""),
        ai_preferred_option_id: String(anchor.ai_preferred_option_id || ""),
        selected_option_id: String(anchor.selected_option_id || ""),
        selected_is_ai_preferred: anchor.selected_is_ai_preferred === true,
        preferred_option_id: String(anchor.selected_option_id || anchor.preferred_option_id || ""),
        selected_option_title: String(selectedOption.title || selectedOption.name || "已选候选方案"),
        candidate_simulation_seed: anchor.candidate_simulation_seed || null,
      },
    });
  };

  const openObservationFromDecision = (decisionPackage, branch, launchTrigger = null) => {
    observationRestoreFocusRef.current = launchTrigger || document.activeElement;
    const anchor = decisionPackage?.anchor || {};
    const event = branch?.event || {};
    const portfolio = branch?.portfolio || event.resource_snapshot || {};
    setObservationLineageSource({
      package_id: String(decisionPackage?.package_id || anchor.user_decision_id || ""),
      user_decision_id: String(anchor.user_decision_id || ""),
      artifact_id: String(anchor.artifact_id || ""),
      artifact_version: Number(anchor.artifact_version) || 0,
      decision_version: String(anchor.decision_version || ""),
      ai_preferred_option_id: String(anchor.ai_preferred_option_id || ""),
      selected_option_id: String(anchor.selected_option_id || ""),
      selected_is_ai_preferred: anchor.selected_is_ai_preferred === true,
      selected_option_title: String(anchor.selected_option?.title || "已选候选方案"),
      source_portfolio_id: String(event.resource_id || portfolio.id || ""),
      source_portfolio_version: Number(branch?.revision || event.resource_revision) || 0,
      source_portfolio_name: String(portfolio.name || "已确认模拟组合"),
    });
    setObservationOpen(true);
  };

  const openReflection = (reflection, launchTrigger = null) => {
    reflectionRestoreFocusRef.current = launchTrigger || document.activeElement;
    setEditingReflection(reflection);
  };

  const savePaperPortfolio = async (form) => {
    setPaperPortfolioLoading(true);
    try {
      const payload = {
        name: form.name,
        positions: form.positions,
        budgets: form.budgets,
        stress_scenarios: form.stress_scenarios,
        ...(form.id ? { expected_version: form.expected_version } : {}),
        ...(!form.id && form.user_decision_id ? {
          user_decision_id: form.user_decision_id,
          derivation_note: form.derivation_note,
        } : {}),
        ...(form.candidate_simulation_confirmation ? {
          candidate_simulation_confirmation: form.candidate_simulation_confirmation,
        } : {}),
      };
      const data = form.id
        ? await api.updatePaperPortfolio(room.id, form.id, payload)
        : await api.createPaperPortfolio(room.id, payload);
      replacePaperPortfolio(data.portfolio);
      syncConvergence(room.id);
      await refreshDecisionPackages(room.id);
      return data.portfolio;
    } catch (requestError) {
      setError(requestError.message);
      throw requestError;
    } finally {
      setPaperPortfolioLoading(false);
    }
  };

  const evaluatePaperPortfolio = async (portfolio) => {
    if (paperPortfolioLoading) return;
    setPaperPortfolioLoading(true);
    try {
      const data = await api.evaluatePaperPortfolio(room.id, portfolio.id, portfolio.version);
      replacePaperPortfolio(data.portfolio);
      await refreshDecisionPackages(room.id);
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      setPaperPortfolioLoading(false);
    }
  };

  const confirmPaperPortfolio = async (portfolio) => {
    if (paperPortfolioLoading) return;
    setPaperPortfolioLoading(true);
    try {
      const data = await api.confirmPaperPortfolio(room.id, portfolio.id, portfolio.version);
      replacePaperPortfolio(data.portfolio);
      syncConvergence(room.id);
      await refreshDecisionPackages(room.id);
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      setPaperPortfolioLoading(false);
    }
  };

  const runPaperPortfolioWalkForward = async (portfolio, config) => {
    if (
      walkForwardLoadingByPortfolio[portfolio.id]
      || walkForwardOperationRef.current.has(portfolio.id)
    ) return;
    walkForwardOperationRef.current.add(portfolio.id);
    setWalkForwardLoadingByPortfolio((current) => ({ ...current, [portfolio.id]: true }));
    setWalkForwardErrorsByPortfolio((current) => ({ ...current, [portfolio.id]: "" }));
    try {
      const data = await api.runPaperPortfolioWalkForward(
        room.id,
        portfolio.id,
        buildWalkForwardRequestPayload(
          portfolio.version,
          config,
          portfolio.candidate_simulation_contract,
        ),
      );
      const run = data.walk_forward_run;
      if (!run?.id || !run.result) throw new Error("固定纸面组合历史滚动回放未返回可审计结果。");
      setWalkForwardRunsByPortfolio((current) => ({
        ...current,
        [portfolio.id]: [
          run,
          ...(current[portfolio.id] || []).filter((item) => item.id !== run.id),
        ].slice(0, 20),
      }));
      await refreshDecisionPackages(room.id);
    } catch (requestError) {
      setWalkForwardErrorsByPortfolio((current) => ({
        ...current,
        [portfolio.id]: requestError.message,
      }));
    } finally {
      walkForwardOperationRef.current.delete(portfolio.id);
      setWalkForwardLoadingByPortfolio((current) => ({ ...current, [portfolio.id]: false }));
    }
  };

  const previewCandidateComparison = (runIds) => {
    const targetRoomId = String(storageRoomId || "");
    if (!targetRoomId || storageRoomIdRef.current !== targetRoomId) {
      return Promise.resolve({ status: "skipped", started: false, applied: false });
    }
    const targetContext = `${targetRoomId}|${paperPortfolioFingerprint}`;
    let payload;
    try {
      payload = buildCandidateComparisonRequest(runIds);
    } catch (requestError) {
      setCandidateComparison(null);
      setCandidateComparisonError(requestError.message);
      return Promise.resolve({ status: "error", started: false, applied: false });
    }
    setCandidateComparison(null);
    setCandidateComparisonError("");
    return candidateComparisonRequestRef.current.run({
      request: (signal) => api.previewCandidateComparison(targetRoomId, payload, signal),
      onSuccess: (data) => {
        if (candidateComparisonContextRef.current !== targetContext) return;
        if (data.comparison?.room_id !== targetRoomId) {
          setCandidateComparison(null);
          setCandidateComparisonError("候选比较响应的房间归属不一致，结果已隐藏。");
          return;
        }
        const returnedRunIds = Array.isArray(data.comparison?.selected_run_ids)
          ? data.comparison.selected_run_ids
          : [];
        if (
          returnedRunIds.length !== payload.run_ids.length
          || returnedRunIds.some((runId, index) => runId !== payload.run_ids[index])
        ) {
          setCandidateComparison(null);
          setCandidateComparisonError("候选比较响应与本次选择不一致，结果已隐藏。");
          return;
        }
        setCandidateComparison(data.comparison || null);
      },
      onError: (requestError) => {
        if (candidateComparisonContextRef.current !== targetContext) return;
        setCandidateComparison(null);
        setCandidateComparisonError(requestError.message);
      },
      onLoadingChange: setCandidateComparisonLoading,
    });
  };

  const saveReflection = async (reflection, form) => {
    try {
      const data = await api.updateReflection(room.id, reflection.observation_id, {
        ...form,
        expected_version: reflection.version,
      });
      replaceReflection(data.reflection);
      return data.reflection;
    } catch (requestError) {
      setError(requestError.message);
      throw requestError;
    }
  };

  const confirmReflection = async (reflection) => {
    try {
      const data = await api.confirmReflection(room.id, reflection.observation_id, reflection.version);
      replaceReflection(data.reflection);
      return data.reflection;
    } catch (requestError) {
      setError(requestError.message);
      throw requestError;
    }
  };

  const openArtifact = (artifact, launchTrigger = null) => {
    artifactRestoreFocusRef.current = launchTrigger || document.activeElement;
    setEditingArtifact(artifact);
  };

  const generateArtifact = async (synthesizerMemberId = "", launchTrigger = null) => {
    if (!room || artifactLoading || roundBusy) return;
    artifactRestoreFocusRef.current = launchTrigger || document.activeElement;
    setArtifactLoading(true);
    try {
      const latestRound = active?.latest_round;
      const roundId = latestRound && !["RUNNING", "CANCELLED"].includes(latestRound.status) ? latestRound.id : "";
      const data = await api.generateArtifact(room.id, roundId, synthesizerMemberId);
      setActive((current) => ({
        ...current,
        artifacts: [
          data.artifact,
          ...(current.artifacts || []).filter((artifact) => artifact.id !== data.artifact.id),
        ],
      }));
      setEditingArtifact(data.artifact);
      syncConvergence(room.id);
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      setArtifactLoading(false);
    }
  };

  const replaceArtifact = (artifact) => {
    setActive((current) => ({
      ...current,
      artifacts: (current.artifacts || []).map((item) => item.id === artifact.id ? artifact : item),
    }));
  };

  const saveArtifact = async (artifact, { keepOpen = false } = {}) => {
    try {
      const data = await api.updateArtifact(room.id, artifact.id, {
        expected_version: artifact.version,
        title: artifact.title,
        content: artifact.content,
      });
      replaceArtifact(data.artifact);
      if (!keepOpen) setEditingArtifact(null);
      syncConvergence(room.id);
      return data.artifact;
    } catch (requestError) {
      setError(requestError.message);
      return null;
    }
  };

  const confirmArtifact = async (artifact) => {
    if (artifactConfirmRef.current) return;
    artifactConfirmRef.current = true;
    let savedDraft = null;
    try {
      const saved = await api.updateArtifact(room.id, artifact.id, {
        expected_version: artifact.version,
        title: artifact.title,
        content: artifact.content,
      });
      savedDraft = saved.artifact;
      replaceArtifact(saved.artifact);
      const confirmed = await api.confirmArtifact(room.id, artifact.id, saved.artifact.version);
      replaceArtifact(confirmed.artifact);
      setEditingArtifact((current) => (
        current?.id === artifact.id ? confirmed.artifact : current
      ));
      syncConvergence(room.id);
    } catch (requestError) {
      if (savedDraft) {
        setEditingArtifact((current) => (
          current?.id === artifact.id ? savedDraft : current
        ));
      }
      setError(requestError.message);
    } finally {
      artifactConfirmRef.current = false;
    }
  };

  const createArtifactUserDecision = async (
    artifact,
    action,
    rationale,
    selectedOptionId = "",
  ) => {
    const roomId = room?.id;
    if (!roomId) throw new Error("当前没有可提交决定的房间。");
    try {
      const payload = buildArtifactUserDecisionRequest(artifact, {
        action,
        rationale,
        selectedOptionId,
      });
      const data = await api.createArtifactUserDecision(roomId, artifact.id, payload);
      if (!data.artifact?.id) throw new Error("用户决定已提交，但服务端没有返回对应产物。");
      const acceptanceIncluded = Object.hasOwn(data, "storage_sample_acceptance");
      setActive((current) => applyArtifactUserDecisionResponse(current, roomId, data));
      setEditingArtifact((current) => (
        current?.id === data.artifact.id ? data.artifact : current
      ));
      const refreshTasks = [refreshDecisionPackages(roomId)];
      if (!data.convergence || !acceptanceIncluded) {
        refreshTasks.push(
          refreshConvergence(roomId).catch((requestError) => {
            setError(`最终决定已提交，但验收状态刷新失败：${requestError.message}`);
            return null;
          }),
        );
      }
      await Promise.all(refreshTasks);
      return data;
    } catch (requestError) {
      setError(() => requestError.message || "用户最终决定提交失败");
      throw requestError;
    }
  };

  const pauseRound = async () => {
    const targetRoomId = String(room?.id || "");
    const control = streamControlFor(targetRoomId);
    const targetRoundId = String(roundState.roundId || control.roundId || "");
    if (!targetRoomId || !targetRoundId || !roundState.running || roundState.pausing || control.transition) return;
    control.transition = "pause";
    control.pauseRequested = true;
    setRuntimeField(targetRoomId, "streamError", "");
    setRoundStateForRoom(targetRoomId, (current) => ({ ...current, pausing: true }));
    try {
      const data = await api.pauseRound(targetRoomId, targetRoundId);
      updateActiveRoom(targetRoomId, (current) => ({
        ...current,
        latest_round: data.round || current.latest_round,
        pending_round: String(data.round?.status || "").toUpperCase() === "PAUSED"
          ? data.round
          : current.pending_round,
        pending_round_checkpoint: data.checkpoint || current.pending_round_checkpoint,
      }));
      if (data.round?.status === "PAUSED") {
        control.pauseRequested = false;
        control.terminalSeen = true;
        control.roundId = "";
        updateRuntime(targetRoomId, (runtime) => ({
          ...reduceRoomRuntimeEvent(runtime, { type: "round_paused" }),
          messageNotice: "已暂停在安全检查点。已完成发言会保留，继续时从下一位开始。",
        }));
      } else {
        setRuntimeField(
          targetRoomId,
          "messageNotice",
          "暂停请求已提交；将在当前成员发言结束后的安全检查点暂停，已完成内容会保留。",
        );
      }
    } catch (requestError) {
      control.pauseRequested = false;
      if (!control.terminalSeen) {
        setRoundStateForRoom(targetRoomId, (current) => ({ ...current, pausing: false }));
      }
      setRuntimeField(targetRoomId, "streamError", requestError.message || "暂停请求提交失败");
    } finally {
      if (control.transition === "pause") control.transition = "";
    }
  };

  const endPendingRound = async () => {
    const targetRoomId = String(room?.id || "");
    const targetRoundId = String(pendingRound?.id || "");
    const control = streamControlFor(targetRoomId);
    if (!targetRoomId || !targetRoundId || !roundAvailability.canEnd || roundBusy || providerRoutingBusy || roundCancelBusy) return;
    if (control.controller || control.transition) return;
    const confirmed = window.confirm("确定结束这轮暂停讨论吗？已完成的消息和审计记录会保留，但本轮将不能继续恢复。");
    if (!confirmed) return;
    control.transition = "cancel";
    setRuntimeField(targetRoomId, "roundCancelBusy", true);
    setRuntimeField(targetRoomId, "streamError", "");
    try {
      const data = await api.cancelRound(targetRoomId, targetRoundId);
      updateActiveRoom(targetRoomId, (current) => ({
        ...current,
        latest_round: data.round || current.latest_round,
        pending_round: null,
        pending_round_checkpoint: null,
      }));
      control.pauseRequested = false;
      control.terminalSeen = true;
      control.roundId = "";
      updateRuntime(targetRoomId, (runtime) => ({
        ...runtime,
        roundState: emptyRoundState(),
        typingMember: null,
        messageNotice: "本轮已明确结束；已完成消息和审计记录仍然保留，可以开始新一轮。",
      }));
      await refreshRooms(targetRoomId).catch((refreshError) => {
        setRuntimeField(
          targetRoomId,
          "streamError",
          `本轮已结束，但房间状态刷新失败：${refreshError.message}`,
        );
      });
    } catch (requestError) {
      setRuntimeField(targetRoomId, "streamError", requestError.message || "结束暂停轮次失败");
      await refreshRooms(targetRoomId).catch(() => {});
    } finally {
      setRuntimeField(targetRoomId, "roundCancelBusy", false);
      if (control.transition === "cancel") control.transition = "";
    }
  };

  const navigateRail = (section, navigationTrigger = null) => {
    inspectorRestoreFocusRef.current = navigationTrigger || document.activeElement;
    sourceInboxRestoreFocusRef.current = navigationTrigger || document.activeElement;
    setActionOverviewOpen(false);
    setRailSection(section);
    setRoomDrawerOpen(false);
    if (section === "source-inbox") {
      setInspectorOpen(false);
      setSourceInboxOpen(true);
      return;
    }
    setSourceInboxOpen(false);
    if (section === "rooms") {
      setInspectorOpen(false);
      document.querySelector(".room-search input")?.focus();
      return;
    }
    setInspectorOpen(true);
    setInspectorNavigation((current) => ({
      targetId: `inspector-${section}`,
      requestId: current.requestId + 1,
    }));
  };

  const closeSourceInbox = () => {
    setSourceInboxOpen(false);
    setRailSection("rooms");
  };

  const loadRoundExecutionTrace = async ({
    targetRoomId,
    targetRoundId,
    cursor = "",
    append = false,
  }) => {
    if (!targetRoomId || !targetRoundId) return;
    const previousRequest = roundExecutionTraceRequestRef.current;
    previousRequest.controller?.abort();
    const controller = new AbortController();
    const sequence = previousRequest.sequence + 1;
    roundExecutionTraceRequestRef.current = { sequence, controller };
    setRoundExecutionTrace((current) => emptyRoundExecutionTraceState({
      ...(current.roomId === targetRoomId && current.roundId === targetRoundId
        ? current
        : {}),
      open: true,
      roomId: targetRoomId,
      roundId: targetRoundId,
      loading: !append,
      loadingMore: append,
      error: "",
    }));
    try {
      const data = await api.roundExecutionTrace(targetRoomId, targetRoundId, {
        limit: 200,
        cursor,
        signal: controller.signal,
      });
      if (roundExecutionTraceRequestRef.current.sequence !== sequence) return;
      const normalized = normalizeRoundExecutionTrace(data.trace);
      if (normalized.room_id !== targetRoomId || normalized.round_id !== targetRoundId) {
        throw new TypeError("执行轨迹返回了不同的房间或轮次。 ");
      }
      setRoundExecutionTrace((current) => {
        if (current.roomId !== targetRoomId || current.roundId !== targetRoundId) return current;
        let nextTrace = normalized;
        if (append && current.trace) {
          nextTrace = mergeRoundExecutionTracePages(current.trace, normalized);
        }
        return {
          ...current,
          trace: nextTrace,
          loading: false,
          loadingMore: false,
          error: "",
          stale: false,
        };
      });
    } catch (requestError) {
      if (
        requestError?.name === "AbortError"
        || roundExecutionTraceRequestRef.current.sequence !== sequence
      ) return;
      setRoundExecutionTrace((current) => (
        current.roomId === targetRoomId && current.roundId === targetRoundId
          ? {
            ...current,
            loading: false,
            loadingMore: false,
            error: requestError?.message || "执行轨迹读取失败",
          }
          : current
      ));
    } finally {
      if (roundExecutionTraceRequestRef.current.sequence === sequence) {
        roundExecutionTraceRequestRef.current.controller = null;
        setRoundExecutionTrace((current) => (
          current.roomId === targetRoomId && current.roundId === targetRoundId
            ? { ...current, loading: false, loadingMore: false }
            : current
        ));
      }
    }
  };

  const loadDiscussionAudit = async ({ targetRoomId, targetRoundId }) => {
    if (!targetRoomId || !targetRoundId) return;
    const previousRequest = discussionAuditRequestRef.current;
    previousRequest.controller?.abort();
    const controller = new AbortController();
    const sequence = previousRequest.sequence + 1;
    discussionAuditRequestRef.current = { sequence, controller };
    setDiscussionAudit((current) => emptyDiscussionAuditState({
      ...(current.roomId === targetRoomId && current.roundId === targetRoundId
        ? current
        : {}),
      roomId: targetRoomId,
      roundId: targetRoundId,
      loading: true,
      error: "",
    }));
    try {
      const data = await api.discussionAudit(
        targetRoomId,
        targetRoundId,
        controller.signal,
      );
      if (discussionAuditRequestRef.current.sequence !== sequence) return;
      const normalized = normalizeDiscussionAudit(data.discussion_audit);
      if (normalized.room_id !== targetRoomId || normalized.round_id !== targetRoundId) {
        throw new TypeError("讨论审计返回了不同的房间或轮次。");
      }
      setDiscussionAudit((current) => (
        current.roomId === targetRoomId && current.roundId === targetRoundId
          ? {
            ...current,
            audit: normalized,
            loading: false,
            error: "",
            stale: false,
          }
          : current
      ));
    } catch (requestError) {
      if (
        requestError?.name === "AbortError"
        || discussionAuditRequestRef.current.sequence !== sequence
      ) return;
      setDiscussionAudit((current) => (
        current.roomId === targetRoomId && current.roundId === targetRoundId
          ? {
            ...current,
            loading: false,
            error: requestError?.message || "讨论审计读取失败",
          }
          : current
      ));
    } finally {
      if (discussionAuditRequestRef.current.sequence === sequence) {
        discussionAuditRequestRef.current.controller = null;
        setDiscussionAudit((current) => (
          current.roomId === targetRoomId && current.roundId === targetRoundId
            ? { ...current, loading: false }
            : current
        ));
      }
    }
  };

  const openRoundExecutionTrace = (roundId) => {
    const targetRoomId = String(activeRoomIdRef.current || "");
    const targetRoundId = String(roundId || "");
    if (!targetRoomId || !targetRoundId) return;
    if (window.matchMedia("(max-width: 1180px)").matches) setInspectorOpen(false);
    const canReuseTrace = (
      roundExecutionTrace.roomId === targetRoomId
      && roundExecutionTrace.roundId === targetRoundId
      && roundExecutionTrace.trace
      && !roundExecutionTrace.stale
    );
    if (canReuseTrace) {
      setRoundExecutionTrace((current) => ({ ...current, open: true, error: "" }));
    } else {
      void loadRoundExecutionTrace({ targetRoomId, targetRoundId });
    }
    const canReuseDiscussionAudit = (
      discussionAudit.roomId === targetRoomId
      && discussionAudit.roundId === targetRoundId
      && discussionAudit.audit
      && !discussionAudit.stale
    );
    if (canReuseDiscussionAudit) {
      setDiscussionAudit((current) => ({ ...current, error: "" }));
    } else {
      void loadDiscussionAudit({ targetRoomId, targetRoundId });
    }
  };

  const retryRoundExecutionTrace = () => {
    const targetRoomId = String(roundExecutionTrace.roomId || activeRoomIdRef.current || "");
    const targetRoundId = String(roundExecutionTrace.roundId || "");
    void loadRoundExecutionTrace({ targetRoomId, targetRoundId });
  };

  const retryDiscussionAudit = () => {
    const targetRoomId = String(discussionAudit.roomId || activeRoomIdRef.current || "");
    const targetRoundId = String(discussionAudit.roundId || roundExecutionTrace.roundId || "");
    void loadDiscussionAudit({ targetRoomId, targetRoundId });
  };

  const loadMoreRoundExecutionTrace = () => {
    const cursor = String(roundExecutionTrace.trace?.page?.next_cursor || "");
    if (!cursor || roundExecutionTrace.loading || roundExecutionTrace.loadingMore) return;
    void loadRoundExecutionTrace({
      targetRoomId: roundExecutionTrace.roomId,
      targetRoundId: roundExecutionTrace.roundId,
      cursor,
      append: true,
    });
  };

  const closeRoundExecutionTrace = () => {
    const request = roundExecutionTraceRequestRef.current;
    request.controller?.abort();
    roundExecutionTraceRequestRef.current = {
      sequence: request.sequence + 1,
      controller: null,
    };
    const auditRequest = discussionAuditRequestRef.current;
    auditRequest.controller?.abort();
    discussionAuditRequestRef.current = {
      sequence: auditRequest.sequence + 1,
      controller: null,
    };
    setRoundExecutionTrace((current) => ({
      ...current,
      open: false,
      loading: false,
      loadingMore: false,
    }));
    setDiscussionAudit((current) => ({ ...current, loading: false }));
  };

  const openRoomDrawer = (event) => {
    roomDrawerRestoreFocusRef.current = event?.currentTarget || mobileRoomToggleRef.current;
    setActionOverviewOpen(false);
    setInspectorOpen(false);
    setRoomDrawerOpen(true);
  };

  const toggleInspector = (event) => {
    inspectorRestoreFocusRef.current = event?.currentTarget || document.activeElement;
    setActionOverviewOpen(false);
    setRoomDrawerOpen(false);
    setInspectorOpen((value) => !value);
  };

  const openActionOverview = () => {
    roomDrawerRestoreFocusRef.current = null;
    setRoomDrawerOpen(false);
    setInspectorOpen(false);
    setActionOverviewOpen(true);
  };

  const openActionOverviewRoom = async (roomId) => {
    const targetRoomId = String(roomId || "").trim();
    if (!targetRoomId) return false;
    const loaded = await loadRoom(targetRoomId);
    if (!loaded || activeRoomIdRef.current !== targetRoomId) return false;
    inspectorRestoreFocusRef.current = document.activeElement;
    setRoomDrawerOpen(false);
    setRailSection("artifacts");
    setActionOverviewOpen(false);
    setInspectorOpen(true);
    setInspectorNavigation((current) => ({
      targetId: "inspector-action-desk",
      requestId: current.requestId + 1,
    }));
    return true;
  };

  const selectRoom = (roomId) => {
    const targetRoomId = String(roomId || "");
    roomDrawerRestoreFocusRef.current = null;
    setActionOverviewOpen(false);
    setRoomDrawerOpen(false);
    setInspectorOpen(false);
    setWorkflowOpen(false);
    setRoomSettingsOpen(false);
    resetMessageNavigation(null);
    void loadRoom(targetRoomId).then((loaded) => {
      if (!loaded || activeRoomIdRef.current !== targetRoomId) return;
      globalThis.requestAnimationFrame(() => {
        roundLaunchSuccessFocusRef.current?.focus({ preventScroll: true });
      });
    });
  };

  const openCreateRoom = () => {
    roomDrawerRestoreFocusRef.current = null;
    setActionOverviewOpen(false);
    setRoomDrawerOpen(false);
    setWorkflowOpen(false);
    setRoomSettingsOpen(false);
    setCreateOpen(true);
  };

  const openWorkflowPolicy = () => {
    if (!room) return;
    setRoomDrawerOpen(false);
    setWorkflowOpen(true);
  };

  const openRoomSettings = () => {
    if (!room) return;
    setRoomDrawerOpen(false);
    setRoomSettingsOpen(true);
  };

  const saveRoomSettings = async (settings) => {
    if (!room) throw new Error("当前没有可设置的房间。");
    const roomId = room.id;
    const data = await api.updateRoom(roomId, settings);
    const updatedRoom = data.room;
    setActive((current) => current?.room?.id === roomId
      ? { ...current, room: { ...current.room, ...updatedRoom } }
      : current);
    setRooms((current) => current.map((item) => item.id === roomId
      ? { ...item, ...updatedRoom }
      : item));
    syncConvergence(roomId);
  };

  const previewPluginLifecycle = (payload, signal) => api.previewPluginLifecycle(payload, signal);

  const transitionPluginLifecycle = async (payload) => {
    const data = await api.transitionPluginLifecycle(payload);
    await refreshRooms(activeRoomIdRef.current);
    return data;
  };

  const saveWorkflowPolicy = async (workflowPolicy) => {
    if (!room) throw new Error("当前没有可设置的房间。");
    const roomId = room.id;
    const data = await api.updateRoom(roomId, {
      workflow_policy: workflowPolicy,
      expected_settings_version: room.settings_version,
    });
    const updatedRoom = data.room || (data.workflow_policy
      ? { ...room, workflow_policy: data.workflow_policy }
      : { ...room, workflow_policy: workflowPolicy });
    const convergenceData = data.convergence
      ? { convergence: data.convergence }
      : await api.convergence(roomId);
    setActive((current) => current?.room?.id === roomId ? {
      ...current,
      room: { ...current.room, ...updatedRoom },
      convergence: convergenceData.convergence,
    } : current);
    setRooms((current) => current.map((item) => item.id === roomId
      ? { ...item, ...updatedRoom }
      : item));
  };

  const createRoom = async (form) => {
    try {
      const data = await api.createRoom(form);
      setCreateOpen(false);
      await refreshRooms(data.room.id, { select: true });
    } catch (requestError) {
      setError(requestError.message);
    }
  };

  const saveMember = async (form) => {
    try {
      const data = form.id
        ? await api.updateMember(room.id, form.id, { ...form, expected_version: form.version })
        : await api.addMember(room.id, form);
      setActive((current) => ({
        ...current,
        members: form.id
          ? current.members.map((member) => member.id === data.member.id ? data.member : member)
          : [...current.members, data.member],
      }));
      if (!form.id) {
        setRooms((current) => current.map((item) => item.id === room.id
          ? { ...item, member_count: Number(item.member_count || 0) + 1 }
          : item));
      }
      setEditingMember(null);
      syncConvergence(room.id);
    } catch (requestError) {
      setError(requestError.message);
      if (form.id && /版本已变化|已归档/.test(requestError.message)) {
        setEditingMember(null);
        await refreshRooms(room.id).catch(() => {});
      }
    }
  };

  const archiveMember = async (member) => {
    if (memberLifecycleLocked) {
      setError("当前轮次运行或暂停中，结束后才能归档成员。");
      return;
    }
    try {
      await api.archiveMember(room.id, member.id, member.version);
      setEditingMember(null);
      await refreshRooms(room.id);
    } catch (requestError) {
      setError(requestError.message);
      if (/版本已变化/.test(requestError.message)) {
        setEditingMember(null);
        await refreshRooms(room.id).catch(() => {});
      }
    }
  };

  const restoreMember = async (member) => {
    if (memberLifecycleLocked) {
      setError("当前轮次运行或暂停中，结束后才能恢复成员。");
      return;
    }
    try {
      await api.restoreMember(room.id, member.id, member.version);
      await refreshRooms(room.id);
    } catch (requestError) {
      setError(requestError.message);
      if (/版本已变化/.test(requestError.message)) await refreshRooms(room.id).catch(() => {});
    }
  };

  const moveMember = async (memberId, direction) => {
    const currentIndex = members.findIndex((member) => member.id === memberId);
    const targetIndex = currentIndex + direction;
    if (currentIndex < 0 || targetIndex < 0 || targetIndex >= members.length) return;
    const reordered = [...members];
    [reordered[currentIndex], reordered[targetIndex]] = [reordered[targetIndex], reordered[currentIndex]];
    setActive((current) => ({ ...current, members: reordered }));
    try {
      const data = await api.reorderMembers(
        room.id,
        reordered.map((member) => member.id),
        members.map((member) => member.id),
      );
      setActive((current) => ({ ...current, members: data.members }));
      syncConvergence(room.id);
    } catch (requestError) {
      setError(requestError.message);
      await loadRoom(room.id);
    }
  };

  const stableTimelineLoadOlder = useStableCallback(loadOlderMessages);
  const stableTimelineSearch = useStableCallback(runMessageSearch);
  const stableTimelineSearchMore = useStableCallback(
    () => runMessageSearch({ append: true }),
  );
  const stableTimelineClearSearch = useStableCallback(clearMessageSearch);

  if (loading) return <div className="boot-screen">正在打开 AI 共创室…</div>;

  return (
    <div className={inspectorOpen ? "app-shell inspector-open" : "app-shell"}>
      <IconRail
        activeSection={railSection}
        onNavigate={navigateRail}
        onPreloadInspector={preloadRoomInspector}
        onPreloadSourceInbox={preloadSourceInboxPanel}
      />
      <button
        className={roomDrawerOpen ? "drawer-scrim room-drawer-scrim open" : "drawer-scrim room-drawer-scrim"}
        type="button"
        aria-label="关闭房间列表"
        onClick={() => setRoomDrawerOpen(false)}
      />
      <RoomSidebar
        rooms={rooms}
        activeRoomId={room?.id}
        search={search}
        mobileOpen={roomDrawerOpen}
        mobileModal={mobileRoomDrawer}
        restoreFocusRef={roomDrawerRestoreFocusRef}
        onSearch={setSearch}
        onSelect={selectRoom}
        onCreate={openCreateRoom}
        onOpenActions={openActionOverview}
        onClose={() => setRoomDrawerOpen(false)}
      />
      {actionOverviewActivated ? (
        <Suspense fallback={<DeferredSurfaceFallback label="跨房间行动总览" />}>
          <ActionOverviewDrawer
            open={actionOverviewOpen}
            onClose={() => setActionOverviewOpen(false)}
            onOpenRoom={openActionOverviewRoom}
            restoreFocusRef={mobileRoomToggleRef}
          />
        </Suspense>
      ) : null}
      {sourceInboxActivated ? <Suspense fallback={(
        <DeferredSurfaceFallback
          label="来源收件箱"
          dialog
          open={sourceInboxOpen}
          onClose={closeSourceInbox}
          restoreFocusRef={sourceInboxRestoreFocusRef}
        />
      )}>
        <SourceInboxPanel
          open={sourceInboxOpen}
          rooms={rooms}
          activeRoomId={activeRoomId}
          restoreFocusRef={sourceInboxRestoreFocusRef}
          onClose={closeSourceInbox}
          onRoomAttached={async (roomId) => {
            if (roomId === activeRoomIdRef.current) {
              await loadRoom(roomId);
              return;
            }
            await refreshRooms(activeRoomIdRef.current);
          }}
        />
      </Suspense> : null}
      <main
        className="conversation-panel"
        aria-hidden={(
          (mobileRoomDrawer && roomDrawerOpen)
          || (compactInspector && inspectorOpen)
          || sourceInboxOpen
        ) ? "true" : undefined}
        inert={(
          (mobileRoomDrawer && roomDrawerOpen)
          || (compactInspector && inspectorOpen)
          || sourceInboxOpen
        ) ? "" : undefined}
      >
        <header
          ref={roundLaunchSuccessFocusRef}
          className="conversation-header"
          aria-label={`${room?.title || "AI 共创室"}讨论状态：${roundStatusLabel}`}
          tabIndex={-1}
        >
          <div>
            <button
              ref={mobileRoomToggleRef}
              className="icon-button mobile-room-toggle"
              type="button"
              title="打开房间列表"
              aria-label="打开房间列表"
              aria-expanded={roomDrawerOpen}
              onClick={openRoomDrawer}
            ><PanelLeft size={18} /></button>
            <strong title={room?.title || "AI 共创室"}>{room?.title || "AI 共创室"}</strong>
            <span><Users size={14} />{members.filter((member) => member.enabled).length} 位成员</span>
            <span className={roundBusy ? "status live" : "status"}>{roundStatusLabel}</span>
          </div>
          <div className="header-actions">
            <button
              className="icon-button source-inbox-mobile-entry"
              type="button"
              title="打开来源收件箱"
              aria-label="打开来源收件箱"
              onClick={(event) => navigateRail("source-inbox", event.currentTarget)}
              onFocus={preloadSourceInboxPanel}
              onPointerDown={preloadSourceInboxPanel}
              onPointerEnter={preloadSourceInboxPanel}
            ><Inbox size={18} /></button>
            {roundState.running && (
              <button
                className="secondary"
                onClick={pauseRound}
                disabled={roundState.pausing || !roundState.roundId}
                title="将在当前成员发言结束后的安全检查点暂停；已完成内容会保留。"
              ><Pause size={15} />{roundState.pausing ? "正在暂停…" : "暂停讨论"}</button>
            )}
            <button
              ref={inspectorToggleRef}
              className="secondary inspector-toggle"
              type="button"
              aria-controls="room-inspector-drawer"
              aria-expanded={inspectorOpen}
              onFocus={preloadRoomInspector}
              onClick={toggleInspector}
              onPointerDown={preloadRoomInspector}
              onPointerEnter={preloadRoomInspector}
            ><Menu size={16} />房间信息</button>
            <button className="icon-button" title="房间设置" aria-label="房间设置" onClick={openRoomSettings} disabled={!room}><Settings size={18} /></button>
          </div>
        </header>
        {visibleError && (
          <div className="global-error">
            {visibleError}
            <button onClick={() => {
              setRuntimeField(activeRoomId, "streamError", "");
              setError("");
            }}>关闭</button>
          </div>
        )}
        {pendingIdleChatRequests.length ? (
          <div className="message-notice recovery-notice">
            上次有 {pendingIdleChatRequests.length} 条定向回复在完成前中断，已安全保留。
            <button
              type="button"
              disabled={messageSending}
              onClick={() => resumePendingChatRequest(pendingIdleChatRequests[0].id)}
            >继续回复</button>
          </div>
        ) : null}
        {messageNotice && <div className="message-notice">{messageNotice}<button onClick={() => setMessageNotice("")}>关闭</button></div>}
        <ChatTimeline
          messages={messages}
          members={members}
          directorDecisions={directorDecisions}
          typingMember={typingMember}
          transientErrors={transientErrors}
          historyState={messageHistory}
          searchInput={messageSearchInput}
          searchState={messageSearch}
          onLoadOlder={stableTimelineLoadOlder}
          onSearchInput={setMessageSearchInput}
          onSearch={stableTimelineSearch}
          onSearchMore={stableTimelineSearchMore}
          onClearSearch={stableTimelineClearSearch}
        />
        <Composer
          value={composer}
          onChange={changeComposer}
          onMention={rememberComposerMention}
          onSend={sendMessage}
          onStartRound={startRound}
          onStartChatGPT={startChatGPTCollaboration}
          disabled={messageSending || roundState.pausing || (roundState.running && !roundState.roundId)}
          roundDisabled={roundBusy || roundLaunchOpen || providerRoutingBusy || !canAttemptNewRound}
          roundStatusLabel={roundLaunch.status === "loading"
            ? "读取启动确认"
            : roundLaunch.status === "ready"
              ? "等待你的确认"
              : roundLaunch.status === "error"
                ? "确认单需重试"
                : !canAttemptNewRound && marketGate.required
                  ? marketGate.shortLabel
                  : "先查看启动确认"}
          roundStatusWarning={roundLaunch.status === "error" || !canAttemptNewRound}
          roundStatusTitle={roundLaunchOpen
            ? "完成或关闭当前启动确认单后，才能再次发起。"
            : newRoundBlockReason || "点击后先读取冻结计划，不会立即调用 Provider。"}
          members={members}
          chatGPTDisabled={roundBusy || chatGPTCollaborationOpen}
          chatGPTStatusTitle={chatGPTCollaborationOpen
            ? "ChatGPT 协作席位已打开。"
            : roundBusy
              ? "当前讨论轮次进行中，暂不能打开 ChatGPT 协作席位。"
              : "打开人工 ChatGPT 协作席位；可在弹窗中填写研究问题，不会自动调用 Provider。"}
        />
      </main>
      <button
        className={inspectorOpen ? "drawer-scrim inspector-scrim open" : "drawer-scrim inspector-scrim"}
        type="button"
        aria-label="关闭房间信息"
        onClick={() => setInspectorOpen(false)}
      />
      <div
        ref={inspectorWrapRef}
        id="room-inspector-drawer"
        className={inspectorOpen ? "inspector-wrap open" : "inspector-wrap"}
        role={compactInspector && inspectorOpen ? "dialog" : undefined}
        aria-modal={compactInspector && inspectorOpen ? "true" : undefined}
        aria-label={compactInspector && inspectorOpen ? "房间信息" : undefined}
        aria-hidden={compactInspector && !inspectorOpen ? "true" : undefined}
        inert={compactInspector && !inspectorOpen ? "" : undefined}
        tabIndex={compactInspector && inspectorOpen ? -1 : undefined}
      >
        <div className="inspector-mobile-head">
          <strong>房间信息</strong>
          <button
            ref={inspectorCloseRef}
            className="icon-button"
            type="button"
            aria-label="关闭房间信息"
            onClick={() => setInspectorOpen(false)}
          >
            <X size={18} />
          </button>
        </div>
        {inspectorActivated ? <Suspense fallback={<DeferredSurfaceFallback label="足球只读面板" />}>
          <FootballResearchPanel
            room={room}
            activation={footballResearchActivation}
            roundContextAuthorization={footballRoundContextAuthorization}
            onRoundContextAuthorizationChange={setFootballRoundContextAuthorization}
          />
        </Suspense> : null}
        {inspectorActivated ? <Suspense fallback={<DeferredSurfaceFallback label="股票只读面板" />}>
          <StockResearchPanel
            room={room}
            activation={stockResearchActivation}
            roundContextAuthorization={stockRoundContextAuthorization}
            onRoundContextAuthorizationChange={setStockRoundContextAuthorization}
          />
        </Suspense> : null}
        {inspectorActivated ? <Suspense fallback={<DeferredSurfaceFallback label="房间信息" />}>
          <RoomInspector
          room={room}
          pluginRegistry={pluginRegistry}
          pluginLifecycle={pluginLifecycle}
          members={members}
          archivedMembers={archivedMembers}
          providers={providers}
          templateWorkflowPolicy={roomTemplate?.workflow_policy}
          roundState={roundState}
          directorDecisions={directorDecisions}
          latestRound={latestRound}
          pendingRound={pendingRound}
          pendingRoundCheckpoint={pendingRoundCheckpoint}
          convergence={convergence}
          storageSampleAcceptance={storageSampleAcceptance}
          workflowConfiguration={workflowConfiguration}
          marketSnapshot={marketSnapshot}
          marketStatus={marketStatus}
          marketReadiness={marketReadiness}
          marketLoading={marketLoading}
          marketReadinessLoading={marketReadinessLoading}
          marketGate={marketGate}
          materials={materials}
          artifacts={artifacts}
          artifactLoading={artifactLoading}
          decisionPackages={decisionPackages}
          observations={observations}
          reflections={reflections}
          paperPortfolios={paperPortfolios}
          walkForwardRunsByPortfolio={walkForwardRunsByPortfolio}
          walkForwardLoadingByPortfolio={walkForwardLoadingByPortfolio}
          walkForwardErrorsByPortfolio={walkForwardErrorsByPortfolio}
          candidateComparison={candidateComparison}
          candidateComparisonLoading={candidateComparisonLoading}
          candidateComparisonError={candidateComparisonError}
          observationScorecard={observationScorecard}
          observationLoading={observationLoading}
          paperPortfolioLoading={paperPortfolioLoading}
          onEditMember={setEditingMember}
          onAddMember={() => setEditingMember({})}
          onMoveMember={moveMember}
          onViewMemberHistory={setMemberHistoryTarget}
          onRestoreMember={restoreMember}
          onEditWorkflowPolicy={openWorkflowPolicy}
          onStartRound={startRound}
          onPause={pauseRound}
          onResumeRound={resumePausedRound}
          onEndRound={endPendingRound}
          onRefreshMarket={refreshMarket}
          onRefreshMarketReadiness={refreshMarketReadiness}
          onFreezeOfficialEvidence={freezeOfficialEvidence}
          onAddOfficialSupplement={openOfficialSupplement}
          onAddMaterial={() => { setMaterialVersions([]); setEditingMaterial({}); }}
          onEditMaterial={openMaterial}
          onGenerateArtifact={generateArtifact}
          onEditArtifact={openArtifact}
          onFillActionDeskComposer={fillActionDeskComposer}
          onAddObservation={(launchTrigger) => {
            observationRestoreFocusRef.current = launchTrigger || document.activeElement;
            setObservationLineageSource(null);
            setObservationOpen(true);
          }}
          onAddObservationFromDecision={openObservationFromDecision}
          onBindObservationDecisionLineage={bindObservationDecisionLineage}
          onConfirmObservation={confirmObservation}
          onReconcileObservations={reconcileObservations}
          onEditReflection={openReflection}
          onAddPaperPortfolio={openPaperPortfolio}
          onAddPaperPortfolioFromDecision={openPaperPortfolioFromDecision}
          onEditPaperPortfolio={openPaperPortfolio}
          onConfirmPaperPortfolio={confirmPaperPortfolio}
          onEvaluatePaperPortfolio={evaluatePaperPortfolio}
          onRunPaperPortfolioWalkForward={runPaperPortfolioWalkForward}
          onCompareCandidates={previewCandidateComparison}
          onRouteMembers={routeEnabledMembers}
          onRunProviderPreflight={runProviderPreflight}
          roundFocusAuthorization={roundFocusAuthorization}
          onFillRoundFocusObjective={fillRoundFocusObjective}
          roundFocusRequired={roundFocusRequired}
          roundFocusReady={activeRoundFocusAuthorization.valid}
          routingBusy={providerRoutingBusy || roundLaunchOpen}
          roundProviderReady
          roundProviderBlockReason={roundProviderBlockReason}
          providerPreflightState={providerPreflightState}
          roundExecutionTraceState={roundExecutionTrace}
          onOpenRoundExecutionTrace={openRoundExecutionTrace}
          endingRound={roundCancelBusy}
          scrollTargetId={inspectorNavigation.targetId}
          scrollRequestId={inspectorNavigation.requestId}
          />
        </Suspense> : null}
      </div>
      {chatGPTCollaborationActivated ? <Suspense fallback={(
        <DeferredSurfaceFallback
          label="ChatGPT 协作席位"
          dialog
          open={chatGPTCollaborationOpen}
          onClose={() => setChatGPTCollaborationOpen(false)}
          restoreFocusRef={chatGPTCollaborationRestoreFocusRef}
        />
      )}>
        <ChatGPTCollaborationDialog
          open={chatGPTCollaborationOpen}
          roomId={room?.id || ""}
          initialObjective={composer.trim() || room?.objective || ""}
          restoreFocusRef={chatGPTCollaborationRestoreFocusRef}
          onClose={() => setChatGPTCollaborationOpen(false)}
        />
      </Suspense> : null}
      {roundLaunchActivated ? <Suspense fallback={(
        <DeferredSurfaceFallback
          label="启动确认"
          dialog
          open={roundLaunchOpen}
          onClose={discardRoundLaunch}
          restoreFocusRef={roundLaunchRestoreFocusRef}
        />
      )}>
        <RoundLaunchDialog
          open={roundLaunchOpen}
          plan={roundLaunch.plan}
          clientRoundRequestId={roundLaunch.clientRoundRequestId}
          loading={roundLaunch.status === "loading"}
          busy={roundLaunch.status === "starting"}
          error={roundLaunch.error}
          restoreFocusRef={roundLaunchRestoreFocusRef}
          onClose={discardRoundLaunch}
          onRetry={retryRoundLaunchPlan}
          onConfirm={confirmRoundLaunch}
        />
      </Suspense> : null}
      {roundExecutionTraceActivated ? <Suspense fallback={<DeferredSurfaceFallback label="执行轨迹" dialog />}>
        <RoundExecutionTraceDialog
          open={roundExecutionTrace.open}
          trace={roundExecutionTrace.trace}
          loading={roundExecutionTrace.loading}
          loadingMore={roundExecutionTrace.loadingMore}
          error={roundExecutionTrace.error}
          stale={roundExecutionTrace.stale}
          discussionAuditState={discussionAudit}
          onClose={closeRoundExecutionTrace}
          onRetry={retryRoundExecutionTrace}
          onRetryDiscussionAudit={retryDiscussionAudit}
          onLoadMore={loadMoreRoundExecutionTrace}
        />
      </Suspense> : null}
      {dialogsActivated ? <Suspense fallback={<DeferredSurfaceFallback label="新建房间" dialog />}>
        <CreateRoomDialog open={createOpen} onClose={() => setCreateOpen(false)} onSubmit={createRoom} restoreFocusRef={mobileRoomToggleRef} templates={templates} capabilityPacks={capabilityPacks} pluginLifecycle={pluginLifecycle} />
      </Suspense> : null}
      {roomSettingsActivated ? <Suspense fallback={<DeferredSurfaceFallback label="房间设置" dialog />}>
        <RoomSettingsDialog
        room={room}
        open={roomSettingsOpen}
        capabilityPacks={capabilityPacks}
        pluginLifecycle={pluginLifecycle}
        members={members}
        roundRunning={roundBusy}
        latestRound={pendingRound}
        onClose={() => setRoomSettingsOpen(false)}
        onSubmit={saveRoomSettings}
        onPreviewPluginLifecycle={previewPluginLifecycle}
          onTransitionPluginLifecycle={transitionPluginLifecycle}
        />
      </Suspense> : null}
      {dialogsActivated ? <Suspense fallback={<DeferredSurfaceFallback label="成员设置" dialog />}>
        <MemberDialog
          member={editingMember}
          room={room}
          open={Boolean(editingMember)}
          onClose={() => setEditingMember(null)}
          onSubmit={saveMember}
          onDelete={archiveMember}
          archiveDisabled={memberLifecycleLocked}
          providers={providers}
          memberTemplates={memberTemplates}
        />
      </Suspense> : null}
      {memberHistoryActivated ? <Suspense fallback={<DeferredSurfaceFallback label="成员版本历史" dialog />}>
        <MemberVersionHistoryDialog
        roomId={room?.id || ""}
        member={memberHistoryTarget}
        open={Boolean(memberHistoryTarget)}
          onClose={() => setMemberHistoryTarget(null)}
        />
      </Suspense> : null}
      {workflowActivated ? <Suspense fallback={<DeferredSurfaceFallback label="讨论流程" dialog />}>
        <WorkflowPolicyDialog
        roomId={room?.id || ""}
        roomTitle={room?.title || ""}
        open={workflowOpen}
        policy={room?.workflow_policy}
        templatePolicy={roomTemplate?.workflow_policy}
        members={members}
        roundRunning={roundBusy}
        onClose={() => setWorkflowOpen(false)}
          onSubmit={saveWorkflowPolicy}
        />
      </Suspense> : null}
      {dialogsActivated ? <Suspense fallback={<DeferredSurfaceFallback label="资料编辑" dialog />}>
        <MaterialDialog
        material={editingMaterial}
        room={room}
        open={Boolean(editingMaterial)}
        onClose={() => { materialVersionsRequestRef.current += 1; setEditingMaterial(null); setMaterialVersions([]); setMaterialVersionsLoading(false); }}
        onSubmit={saveMaterial}
        onFetchUrl={fetchMaterialUrl}
        onImportFile={importMaterialFile}
        onConfirmOfficialAttestation={confirmOfficialAttestation}
        versions={materialVersions}
          versionsLoading={materialVersionsLoading}
        />
      </Suspense> : null}
      {artifactActivated ? <Suspense fallback={<DeferredSurfaceFallback label="产物工作区" dialog />}>
        <ArtifactDialog
        artifact={editingArtifact}
        room={room}
        pluginRegistry={pluginRegistry}
        pluginLifecycle={pluginLifecycle}
        open={Boolean(editingArtifact)}
        restoreFocusRef={artifactRestoreFocusRef}
        messages={messages}
        materials={materials}
        onClose={() => setEditingArtifact(null)}
        onSave={saveArtifact}
        onConfirm={confirmArtifact}
        onUserDecision={createArtifactUserDecision}
          onExport={(artifact) => downloadArtifactMarkdown(artifact, room?.title || "AI 共创室")}
        />
      </Suspense> : null}
      {observationActivated ? <Suspense fallback={<DeferredSurfaceFallback label="观察记录" dialog />}>
        <ObservationDialog
        open={observationOpen}
        materials={materials}
          lineageSource={observationLineageSource}
          restoreFocusRef={observationRestoreFocusRef}
          onClose={() => {
          setObservationOpen(false);
          setObservationLineageSource(null);
        }}
          onSubmit={createObservation}
        />
      </Suspense> : null}
      {reflectionActivated ? <Suspense fallback={<DeferredSurfaceFallback label="复盘记录" dialog />}>
        <ReflectionDialog
        reflection={editingReflection}
        open={Boolean(editingReflection)}
        restoreFocusRef={reflectionRestoreFocusRef}
        onClose={() => setEditingReflection(null)}
        onSave={saveReflection}
          onConfirm={confirmReflection}
        />
      </Suspense> : null}
      {paperPortfolioActivated ? <Suspense fallback={<DeferredSurfaceFallback label="纸面组合" dialog />}>
        <PaperPortfolioDialog
          portfolio={editingPaperPortfolio}
          open={Boolean(editingPaperPortfolio)}
          restoreFocusRef={paperPortfolioRestoreFocusRef}
          onClose={() => setEditingPaperPortfolio(null)}
          onSubmit={savePaperPortfolio}
        />
      </Suspense> : null}
    </div>
  );
}
