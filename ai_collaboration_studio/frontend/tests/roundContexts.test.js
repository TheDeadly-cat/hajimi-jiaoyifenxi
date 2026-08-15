import assert from "node:assert/strict";
import test from "node:test";

import {
  buildRoundContextAuthorizationSet,
  normalizeRoundContextAuthorizationSet,
  roundContextAuthorizationEntry,
} from "../src/roundContexts.js";

function projectRequest() {
  return {
    version: "project_round_focus_authorization_v1",
    artifact_binding: { status: "none" },
    preview_sha256: "a".repeat(64),
    user_confirmed: true,
  };
}

function footballRequest() {
  return {
    version: "football_round_context_request_v1",
    payload: {
      version: "football_research_contract_v1",
      match_identity: { match_id: { value: "match-001" } },
      data_cutoff_utc: "2026-08-12T10:00:00Z",
      contract_sha256: "b".repeat(64),
    },
    authorization: {
      version: "football_round_context_authorization_v1",
      owner_pack_id: "football_research_readonly",
      port_id: "core.football.match_context/v1",
      contract_sha256: "b".repeat(64),
      data_cutoff_utc: "2026-08-12T10:00:00Z",
      match_id: "match-001",
      user_confirmed: true,
    },
  };
}

test("accepts the exact slash-delimited host ports and clones domain requests", () => {
  const request = footballRequest();
  const entry = roundContextAuthorizationEntry(
    "football_research_readonly",
    "core.football.match_context/v1",
    request,
  );

  assert.equal(entry.port_id, "core.football.match_context/v1");
  assert.notEqual(entry.request, request);
  request.authorization.match_id = "drifted";
  assert.equal(entry.request.authorization.match_id, "match-001");
});

test("builds one closed, deterministic provider set for project and football", () => {
  const value = buildRoundContextAuthorizationSet([
    roundContextAuthorizationEntry(
      "project_round_focus",
      "core.round.context/v1",
      projectRequest(),
    ),
    roundContextAuthorizationEntry(
      "football_research_readonly",
      "core.football.match_context/v1",
      footballRequest(),
    ),
  ]);

  assert.equal(value.version, "round_context_authorization_set_v1");
  assert.deepEqual(value.contexts.map((entry) => [entry.owner_pack_id, entry.port_id]), [
    ["football_research_readonly", "core.football.match_context/v1"],
    ["project_round_focus", "core.round.context/v1"],
  ]);
  assert.equal(normalizeRoundContextAuthorizationSet(value).valid, true);
});

test("normalization ignores JSON object key insertion order but rejects provider-order drift", () => {
  const football = roundContextAuthorizationEntry(
    "football_research_readonly",
    "core.football.match_context/v1",
    footballRequest(),
  );
  const project = roundContextAuthorizationEntry(
    "project_round_focus",
    "core.round.context/v1",
    projectRequest(),
  );
  const reorderedKeys = {
    contexts: [{
      request: football.request,
      port_id: football.port_id,
      owner_pack_id: football.owner_pack_id,
      version: football.version,
    }],
    version: "round_context_authorization_set_v1",
  };

  assert.equal(normalizeRoundContextAuthorizationSet(reorderedKeys).valid, true);
  const providerOrderDrift = {
    version: "round_context_authorization_set_v1",
    contexts: [project, football],
  };
  assert.equal(normalizeRoundContextAuthorizationSet(providerOrderDrift).valid, false);
});

test("fails closed for duplicate, extra-field, separator, and capacity violations", () => {
  const entry = roundContextAuthorizationEntry(
    "project_round_focus",
    "core.round.context/v1",
    projectRequest(),
  );
  assert.throws(
    () => buildRoundContextAuthorizationSet([entry, entry]),
    /duplicated/,
  );
  assert.equal(normalizeRoundContextAuthorizationSet({
    version: "round_context_authorization_set_v1",
    contexts: [{ ...entry, extra: true }],
  }).valid, false);
  assert.throws(
    () => roundContextAuthorizationEntry("bad\u001fpack", "core.round.context/v1", {}),
    /invalid/,
  );
  assert.throws(
    () => buildRoundContextAuthorizationSet(Array.from({ length: 65 }, (_, index) => (
      roundContextAuthorizationEntry(`pack_${index}`, `port_${index}/v1`, {})
    ))),
    /capacity/,
  );
});
