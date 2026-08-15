import assert from "node:assert/strict";
import test from "node:test";

import { bindableAiProposals } from "../src/observationLineage.js";


test("only exposes unconfirmed AI proposals from the exact artifact round", () => {
  const base = {
    round_id: "round_current",
    status: "PROPOSED",
    user_confirmed: false,
    confidence_source: "ai",
    created_by: "member_technical",
  };
  const result = bindableAiProposals([
    { ...base, id: "eligible" },
    { ...base, id: "other_round", round_id: "round_old" },
    { ...base, id: "user", created_by: "user", confidence_source: "user" },
    { ...base, id: "confirmed", user_confirmed: true, status: "OPEN" },
    { ...base, id: "unverified", confidence_source: "unverified" },
  ], "round_current");
  assert.deepEqual(result.map((item) => item.id), ["eligible"]);
});


test("fails closed when the decision package omits its frozen round id", () => {
  assert.deepEqual(bindableAiProposals([{
    id: "proposal",
    round_id: "round_current",
    status: "PROPOSED",
    user_confirmed: false,
    confidence_source: "ai",
    created_by: "member_technical",
  }], ""), []);
});
