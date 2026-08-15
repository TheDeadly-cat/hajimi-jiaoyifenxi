export function emptyRoundState(overrides = {}) {
  return {
    running: false,
    pausing: false,
    memberStatus: {},
    roundId: "",
    stage: "",
    directorDecision: null,
    ...overrides,
  };
}

export function emptyRoomRuntime(overrides = {}) {
  return {
    roundState: emptyRoundState(),
    typingMember: null,
    transientErrors: [],
    messageNotice: "",
    messageSending: false,
    roundCancelBusy: false,
    streamError: "",
    ...overrides,
  };
}

export function roomRuntimeFor(runtimeByRoomId, roomId) {
  if (!roomId) return emptyRoomRuntime();
  return runtimeByRoomId?.[roomId] || emptyRoomRuntime();
}

export function updateRoomRuntime(runtimeByRoomId, roomId, updater) {
  if (!roomId) return runtimeByRoomId || {};
  const currentMap = runtimeByRoomId || {};
  const current = roomRuntimeFor(currentMap, roomId);
  const next = typeof updater === "function" ? updater(current) : updater;
  if (!next || next === current) return currentMap;
  return { ...currentMap, [roomId]: next };
}

export function updateSelectedRoomSnapshot(activeSnapshot, roomId, updater) {
  if (String(activeSnapshot?.room?.id || "") !== String(roomId || "")) {
    return activeSnapshot;
  }
  return updater(activeSnapshot);
}

export function shouldApplyRoomRefresh(selectedRoomId, snapshotRoomId, forceSelect = false) {
  return Boolean(forceSelect)
    || String(selectedRoomId || "") === String(snapshotRoomId || "");
}

export function reconcileRoomRuntime(runtime, snapshot, { preserveLocalRunning = false } = {}) {
  const current = runtime || emptyRoomRuntime();
  const latestRound = snapshot?.latest_round || null;
  const latestStatus = String(latestRound?.status || "").toUpperCase();
  if (latestStatus === "RUNNING" && latestRound?.id) {
    return {
      ...current,
      roundState: {
        ...current.roundState,
        running: true,
        pausing: Boolean(latestRound.pause_requested),
        roundId: String(latestRound.id),
      },
    };
  }
  // An attached local stream owns its transient state until its finally block
  // reconciles the authoritative server result. Once detached, a later
  // authoritative terminal snapshot is allowed to release a stale local lock.
  if (preserveLocalRunning && current.roundState?.running) return current;
  return {
    ...current,
    roundState: emptyRoundState(),
    typingMember: null,
  };
}

export function reduceRoomRuntimeEvent(runtime, event, directorDecision = null) {
  const current = runtime || emptyRoomRuntime();
  const memberId = String(event?.member?.id || "");
  if (event?.type === "round_started" || event?.type === "round_resumed") {
    return {
      ...current,
      roundState: {
        ...current.roundState,
        running: true,
        pausing: false,
        roundId: String(event.round?.id || current.roundState.roundId || ""),
      },
    };
  }
  if (event?.type === "director_decision" && directorDecision) {
    const stage = directorDecision.stage || current.roundState.stage || "flexible";
    return {
      ...current,
      roundState: {
        ...current.roundState,
        stage,
        directorDecision: {
          action: directorDecision.action,
          member: event.member || (directorDecision.member_id
            ? { id: directorDecision.member_id, name: directorDecision.member_name }
            : null),
          reason: directorDecision.reason,
          source: directorDecision.source,
          stage,
          workspaceFocus: directorDecision.workspace_focus,
          moderatorContext: directorDecision.moderator_context || null,
        },
      },
    };
  }
  if (event?.type === "speaker_started" && memberId) {
    return {
      ...current,
      typingMember: event.member,
      roundState: {
        ...current.roundState,
        memberStatus: { ...current.roundState.memberStatus, [memberId]: "speaking" },
      },
    };
  }
  if (["message", "speaker_failed", "speaker_skipped"].includes(event?.type) && memberId) {
    const status = event.type === "message"
      ? "done"
      : event.type === "speaker_failed"
        ? "failed"
        : "skipped";
    return {
      ...current,
      typingMember: null,
      roundState: {
        ...current.roundState,
        memberStatus: { ...current.roundState.memberStatus, [memberId]: status },
      },
    };
  }
  if (event?.type === "round_paused" || event?.type === "round_completed") {
    return {
      ...current,
      typingMember: null,
      roundState: {
        ...current.roundState,
        running: false,
        pausing: false,
        roundId: "",
        stage: "",
      },
    };
  }
  return current;
}
