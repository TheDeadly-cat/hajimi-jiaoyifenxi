import assert from "node:assert/strict";
import test from "node:test";

import { isModeratorSelectionMissing } from "../src/moderatorSelection.js";

test("marks an archived or otherwise missing moderator as stale", () => {
  assert.equal(
    isModeratorSelectionMissing("member_archived", [{ id: "member_active" }]),
    true,
  );
});

test("keeps automatic and present moderator selections valid", () => {
  assert.equal(isModeratorSelectionMissing("", []), false);
  assert.equal(
    isModeratorSelectionMissing("member_present", [{ id: "member_present", enabled: false }]),
    false,
  );
});
