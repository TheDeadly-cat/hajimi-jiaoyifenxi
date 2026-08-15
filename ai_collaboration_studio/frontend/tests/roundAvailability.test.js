import test from "node:test";
import assert from "node:assert/strict";
import { deriveRoundAvailability } from "../src/roundAvailability.js";

test("uses pending_round instead of latest_round as the authoritative lock", () => {
  const availability = deriveRoundAvailability({
    latest_round: { id: "round_old", status: "PAUSED" },
    round_checkpoint: { round_id: "round_old" },
    pending_round: null,
    pending_round_checkpoint: null,
  });

  assert.equal(availability.hasPendingRound, false);
  assert.equal(availability.canResume, false);
  assert.equal(availability.canEnd, false);
  assert.equal(availability.blockReason, "");
});

test("allows resume and explicit end for a paused round with a matching checkpoint", () => {
  const checkpoint = { round_id: "round_pending", step_number: 4 };
  const availability = deriveRoundAvailability({
    pending_round: { id: "round_pending", status: "PAUSED" },
    pending_round_checkpoint: checkpoint,
  });

  assert.equal(availability.hasPendingRound, true);
  assert.equal(availability.pausedRoundPending, true);
  assert.equal(availability.canResume, true);
  assert.equal(availability.canEnd, true);
  assert.equal(availability.pendingRoundCheckpoint, checkpoint);
  assert.match(availability.blockReason, /继续或结束/);
});

test("keeps an end path when a paused round has no usable checkpoint", () => {
  for (const checkpoint of [null, { round_id: "another_round" }]) {
    const availability = deriveRoundAvailability({
      pending_round: { id: "round_pending", status: "PAUSED" },
      pending_round_checkpoint: checkpoint,
    });

    assert.equal(availability.canResume, false);
    assert.equal(availability.canEnd, true);
    assert.equal(availability.pendingRoundCheckpoint, null);
    assert.match(availability.blockReason, /缺少可恢复检查点/);
  }
});

test("locks a non-paused pending round without exposing invalid actions", () => {
  const availability = deriveRoundAvailability({
    pending_round: { id: "round_running", status: "RUNNING" },
    pending_round_checkpoint: { round_id: "round_running" },
  });

  assert.equal(availability.hasPendingRound, true);
  assert.equal(availability.pausedRoundPending, false);
  assert.equal(availability.canResume, false);
  assert.equal(availability.canEnd, false);
  assert.match(availability.blockReason, /尚未结束/);
});
