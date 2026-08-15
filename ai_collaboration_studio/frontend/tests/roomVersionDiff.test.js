import assert from "node:assert/strict";
import test from "node:test";

import { buildRoomVersionDiff, roomVersionSnapshot } from "../src/roomVersionDiff.js";

test("unwraps exact room-version API records", () => {
  const snapshot = { settings_version: 2, title: "第二版" };
  assert.equal(roomVersionSnapshot({ room_version: { snapshot } }), snapshot);
});

test("reports routing, policy and capability-pack changes", () => {
  const diff = buildRoomVersionDiff(
    { room_version: { snapshot: {
      title: "研究室",
      discussion_mode: "sequential",
      moderator_member_id: "member_a",
      idle_response_mode: "mention_only",
      workflow_policy: { stage_order: ["analysis", "risk"] },
      capability_pack_ids: ["base"],
      capabilities: ["evidence"],
    } } },
    { room_version: { snapshot: {
      title: "研究室",
      discussion_mode: "dynamic",
      moderator_member_id: "member_b",
      idle_response_mode: "moderator_auto",
      workflow_policy: { stage_order: ["analysis", "debate", "risk"] },
      capability_pack_ids: ["base", "market"],
      capabilities: ["evidence", "market_data"],
    } } },
  );

  assert.equal(diff.changed, true);
  assert.deepEqual(
    diff.fieldChanges.map((item) => item.key),
    ["discussion_mode", "moderator_member_id", "idle_response_mode", "workflow_policy"],
  );
  assert.deepEqual(diff.capabilityPacks.added, ["market"]);
  assert.deepEqual(diff.capabilities.added, ["market_data"]);
});

test("object key order does not create a false workflow-policy change", () => {
  const diff = buildRoomVersionDiff(
    { workflow_policy: { b: 2, a: { y: 2, x: 1 } } },
    { workflow_policy: { a: { x: 1, y: 2 }, b: 2 } },
  );
  assert.equal(diff.changed, false);
});
