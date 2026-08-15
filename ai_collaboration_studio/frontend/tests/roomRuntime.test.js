import assert from "node:assert/strict";
import test from "node:test";

import {
  emptyRoomRuntime,
  reconcileRoomRuntime,
  reduceRoomRuntimeEvent,
  roomRuntimeFor,
  shouldApplyRoomRefresh,
  updateSelectedRoomSnapshot,
  updateRoomRuntime,
} from "../src/roomRuntime.js";

test("round runtime updates stay isolated by room id", () => {
  const initial = {
    room_a: emptyRoomRuntime(),
    room_b: emptyRoomRuntime(),
  };
  const updated = updateRoomRuntime(initial, "room_a", (runtime) => ({
    ...runtime,
    roundState: { ...runtime.roundState, running: true, roundId: "round_a" },
  }));

  assert.equal(roomRuntimeFor(updated, "room_a").roundState.running, true);
  assert.equal(roomRuntimeFor(updated, "room_a").roundState.roundId, "round_a");
  assert.deepEqual(roomRuntimeFor(updated, "room_b"), initial.room_b);
});

test("round events only change the runtime they are reduced into", () => {
  const roomA = reduceRoomRuntimeEvent(emptyRoomRuntime(), {
    type: "speaker_started",
    member: { id: "member_a", name: "A" },
  });
  const roomB = emptyRoomRuntime();

  assert.equal(roomA.typingMember.id, "member_a");
  assert.equal(roomA.roundState.memberStatus.member_a, "speaking");
  assert.equal(roomB.typingMember, null);
  assert.deepEqual(roomB.roundState.memberStatus, {});
});

test("background room events and refreshes cannot replace another selected room", () => {
  const selectedRoom = { room: { id: "room_b" }, messages: [] };
  const unchanged = updateSelectedRoomSnapshot(selectedRoom, "room_a", (current) => ({
    ...current,
    messages: [{ id: "message_from_a" }],
  }));

  assert.equal(unchanged, selectedRoom);
  assert.equal(shouldApplyRoomRefresh("room_b", "room_a"), false);
  assert.equal(shouldApplyRoomRefresh("room_b", "room_a", true), true);
});

test("authoritative snapshots restore a room-local running lock", () => {
  const reconciled = reconcileRoomRuntime(emptyRoomRuntime(), {
    latest_round: { id: "round_server", status: "RUNNING", pause_requested: true },
  });

  assert.equal(reconciled.roundState.running, true);
  assert.equal(reconciled.roundState.pausing, true);
  assert.equal(reconciled.roundState.roundId, "round_server");
});

test("a detached room releases a stale local lock from a terminal snapshot", () => {
  const localRunning = emptyRoomRuntime({
    roundState: {
      ...emptyRoomRuntime().roundState,
      running: true,
      roundId: "round_local",
    },
  });
  const terminalSnapshot = {
    latest_round: { id: "round_local", status: "COMPLETED" },
  };

  const released = reconcileRoomRuntime(localRunning, terminalSnapshot);
  const preserved = reconcileRoomRuntime(localRunning, terminalSnapshot, {
    preserveLocalRunning: true,
  });

  assert.equal(released.roundState.running, false);
  assert.equal(released.roundState.roundId, "");
  assert.equal(preserved.roundState.running, true);
  assert.equal(preserved.roundState.roundId, "round_local");
});

test("current director runtime keeps moderator attribution for the inspector", () => {
  const moderatorContext = {
    version: "director_moderator_context_v1",
    decision_authority: "moderator_model",
    model_used: true,
    discussion_mode: "dynamic",
    member_id: "moderator_1",
    member_name: "主持人",
    identity: "流程守门人",
    member_version: 2,
    provider: "deepseek",
    model: "deepseek-chat",
  };
  const runtime = reduceRoomRuntimeEvent(
    emptyRoomRuntime(),
    { type: "director_decision", member: { id: "member_1", name: "研究员" } },
    {
      action: "speak",
      member_id: "member_1",
      member_name: "研究员",
      source: "ai",
      stage: "analysis",
      moderator_context: moderatorContext,
    },
  );

  assert.deepEqual(runtime.roundState.directorDecision.moderatorContext, moderatorContext);
});
