import assert from "node:assert/strict";
import test from "node:test";

import { resolveComposerMentions } from "../src/composerMentions.js";

const member = (id, name, version = 1) => ({ id, name, version, enabled: true });

test("a manually typed unique member name resolves to one structured target", () => {
  const result = resolveComposerMentions(
    "@研究员 请检查证据",
    [member("member_a", "研究员", 3), member("member_b", "主持人")],
    [],
  );

  assert.deepEqual(result, {
    mentions: [{ member_id: "member_a", expected_member_version: 3 }],
    ambiguousNames: [],
  });
});

test("a manually typed duplicate name fails closed instead of targeting everyone", () => {
  const result = resolveComposerMentions(
    "@研究员 请回应",
    [member("member_a", "研究员"), member("member_b", "研究员")],
    [],
  );

  assert.deepEqual(result.mentions, []);
  assert.deepEqual(result.ambiguousNames, ["研究员"]);
});

test("an explicit menu selection disambiguates duplicate names by member id", () => {
  const result = resolveComposerMentions(
    "@研究员 请回应",
    [member("member_a", "研究员", 2), member("member_b", "研究员", 5)],
    [{
      member_id: "member_b",
      name: "研究员",
      expected_member_version: 5,
    }],
  );

  assert.deepEqual(result, {
    mentions: [{ member_id: "member_b", expected_member_version: 5 }],
    ambiguousNames: [],
  });
});

test("a longer member name does not also target its shorter prefix", () => {
  const result = resolveComposerMentions(
    "@研究员甲 请先回答",
    [member("member_short", "研究员", 2), member("member_long", "研究员甲", 4)],
    [],
  );

  assert.deepEqual(result, {
    mentions: [{ member_id: "member_long", expected_member_version: 4 }],
    ambiguousNames: [],
  });
});

test("a stale explicit selection is removed when only a longer name remains in the text", () => {
  const result = resolveComposerMentions(
    "@研究员甲 请回应",
    [member("member_short", "研究员", 2), member("member_long", "研究员甲", 4)],
    [{ member_id: "member_short", name: "研究员", expected_member_version: 2 }],
  );

  assert.deepEqual(result, {
    mentions: [{ member_id: "member_long", expected_member_version: 4 }],
    ambiguousNames: [],
  });
});

test("punctuation terminates an exact mention and preserves mention order", () => {
  const result = resolveComposerMentions(
    "请 @风控经理，随后 @主持人。",
    [member("host", "主持人", 3), member("risk", "风控经理", 5)],
    [],
  );

  assert.deepEqual(result, {
    mentions: [
      { member_id: "risk", expected_member_version: 5 },
      { member_id: "host", expected_member_version: 3 },
    ],
    ambiguousNames: [],
  });
});

test("letters and common name punctuation do not terminate a shorter mention", () => {
  const result = resolveComposerMentions(
    "@研究员A @研究员-甲 @研究员.甲",
    [member("member_short", "研究员", 2)],
    [],
  );

  assert.deepEqual(result, { mentions: [], ambiguousNames: [] });
});
