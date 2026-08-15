import test from "node:test";
import assert from "node:assert/strict";

import {
  CORE_DISCUSSION_PROTOCOL_PACK_ID,
  ROOM_CAPABILITIES,
  isCoreDiscussionProtocolPack,
  roomDomainCapabilityLabels,
  splitCapabilityPacks,
} from "../src/roomCapabilities.js";

test("legacy turn-contract pack is read-only core protocol, never an optional pack", () => {
  const legacyCatalogEntry = {
    id: CORE_DISCUSSION_PROTOCOL_PACK_ID,
    title: "Auditable response graph",
    capabilities: [ROOM_CAPABILITIES.auditableResponseGraph],
  };
  const optionalPack = {
    id: "structured_project_research",
    title: "Project research",
  };

  const split = splitCapabilityPacks([legacyCatalogEntry, optionalPack]);

  assert.equal(isCoreDiscussionProtocolPack(legacyCatalogEntry), true);
  assert.deepEqual(split.coreProtocols, [legacyCatalogEntry]);
  assert.deepEqual(split.optionalDomainPacks, [optionalPack]);
});

test("backend catalog metadata also marks the formal-round protocol as core", () => {
  const pack = {
    id: "future_core_protocol_id",
    system_managed: true,
    scope: "formal_round_core",
  };

  assert.deepEqual(splitCapabilityPacks([pack]), {
    coreProtocols: [pack],
    optionalDomainPacks: [],
  });
});

test("auditable response graph stays a core label even when room has no legacy pack ID", () => {
  const roomWithoutLegacyPack = {
    capability_pack_ids: [],
    capabilities: ["collaboration.chat", "materials.shared"],
  };
  const roomWithCoreCapability = {
    ...roomWithoutLegacyPack,
    capabilities: [
      ...roomWithoutLegacyPack.capabilities,
      ROOM_CAPABILITIES.auditableResponseGraph,
    ],
  };

  assert.deepEqual(roomDomainCapabilityLabels(roomWithoutLegacyPack), ["通用协作"]);
  assert.deepEqual(roomDomainCapabilityLabels(roomWithCoreCapability), ["通用协作"]);
  // The frontend never treats the absence of the historical pack ID as an
  // opt-out; new formal-round enforcement is owned by the backend snapshot.
  assert.deepEqual(roomWithoutLegacyPack.capability_pack_ids, []);
});
