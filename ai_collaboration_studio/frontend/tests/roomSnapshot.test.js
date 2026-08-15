import assert from "node:assert/strict";
import test from "node:test";

import {
  applyMaterialToRoomSnapshot,
  applyOfficialAttestationToRoomSnapshot,
  officialAttestationsFromRoomResponse,
} from "../src/roomSnapshot.js";

test("room switching preserves staged official attestations from the room response", () => {
  const staged = [{
    id: "attestation_staged",
    material_id: "material_sndk",
    state: "staged",
    integrity_ready: true,
  }];

  assert.equal(
    officialAttestationsFromRoomResponse({ official_attestations: staged }),
    staged,
  );
});

test("legacy room responses without official attestations map to an empty list", () => {
  assert.deepEqual(officialAttestationsFromRoomResponse({}), []);
  assert.deepEqual(officialAttestationsFromRoomResponse(null), []);
});

test("late material and attestation responses from another room cannot pollute the selected room", () => {
  const selectedRoom = {
    room: { id: "room_b" },
    materials: [{ id: "material_b", version: 1 }],
    official_attestations: [{ id: "attestation_b", state: "staged" }],
  };

  assert.equal(
    applyMaterialToRoomSnapshot(selectedRoom, "room_a", { id: "material_a", version: 2 }),
    selectedRoom,
  );
  assert.equal(
    applyOfficialAttestationToRoomSnapshot(selectedRoom, "room_a", {
      id: "attestation_a",
      status: "CONFIRMED",
      state: "confirmed",
    }),
    selectedRoom,
  );
  assert.deepEqual(selectedRoom.materials.map((item) => item.id), ["material_b"]);
  assert.deepEqual(
    selectedRoom.official_attestations.map((item) => item.id),
    ["attestation_b"],
  );
});
