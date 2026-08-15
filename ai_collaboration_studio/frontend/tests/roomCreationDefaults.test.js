import test from "node:test";
import assert from "node:assert/strict";

import { roomCreationCapabilityPackIds } from "../src/roomCreationDefaults.js";

test("new general rooms prefer creation-only structured turn defaults", () => {
  const template = {
    capability_pack_ids: [],
    creation_default_capability_pack_ids: ["structured_turn_contract_v1"],
  };

  assert.deepEqual(
    roomCreationCapabilityPackIds(template),
    ["structured_turn_contract_v1"],
  );
  assert.deepEqual(template.capability_pack_ids, []);
});

test("creation defaults fall back to template defaults without mutating either list", () => {
  const template = {
    capability_pack_ids: ["storage_research_readonly", "structured_turn_contract_v1"],
  };
  const selected = roomCreationCapabilityPackIds(template);

  selected.pop();
  assert.deepEqual(template.capability_pack_ids, [
    "storage_research_readonly",
    "structured_turn_contract_v1",
  ]);
});

test("invalid or duplicate defaults are normalized for checkbox state", () => {
  assert.deepEqual(roomCreationCapabilityPackIds({
    creation_default_capability_pack_ids: [
      " structured_turn_contract_v1 ",
      "structured_turn_contract_v1",
      "",
      null,
    ],
  }), ["structured_turn_contract_v1"]);
  assert.deepEqual(roomCreationCapabilityPackIds(null), []);
});
