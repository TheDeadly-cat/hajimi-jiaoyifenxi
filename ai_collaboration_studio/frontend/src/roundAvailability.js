function normalizedStatus(round) {
  return String(round?.status || "").trim().toUpperCase();
}

export function deriveRoundAvailability(snapshot) {
  const candidate = snapshot?.pending_round;
  const pendingRound = candidate && String(candidate.id || "").trim() ? candidate : null;
  const checkpoint = snapshot?.pending_round_checkpoint || null;
  const status = normalizedStatus(pendingRound);
  const hasPendingRound = Boolean(pendingRound);
  const pausedRoundPending = hasPendingRound && status === "PAUSED";
  const checkpointMatches = pausedRoundPending
    && String(checkpoint?.round_id || "") === String(pendingRound.id);
  const canResume = Boolean(checkpointMatches);
  const canEnd = pausedRoundPending;

  let blockReason = "";
  if (pausedRoundPending && canResume) {
    blockReason = "当前有一轮讨论停在安全检查点；请先继续或结束该轮，不能用新轮覆盖其恢复入口。";
  } else if (pausedRoundPending) {
    blockReason = "当前有暂停轮次但缺少可恢复检查点；请结束该轮后再开始新一轮。";
  } else if (hasPendingRound) {
    blockReason = "当前有一轮讨论尚未结束，不能开始新一轮。";
  }

  return {
    pendingRound,
    pendingRoundCheckpoint: checkpointMatches ? checkpoint : null,
    status,
    hasPendingRound,
    pausedRoundPending,
    checkpointMatches,
    canResume,
    canEnd,
    blockReason,
  };
}
