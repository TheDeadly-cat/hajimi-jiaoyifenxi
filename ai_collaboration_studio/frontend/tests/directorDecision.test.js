import assert from "node:assert/strict";
import test from "node:test";

import {
  directorModeratorAttribution,
  directorSourceLabel,
  normalizeDirectorDecision,
} from "../src/directorDecision.js";

const moderatorContext = {
  version: "director_moderator_context_v1",
  decision_authority: "moderator_model",
  model_used: true,
  discussion_mode: "dynamic",
  member_id: "member_moderator",
  member_name: "投资委员会主持人",
  identity: "存储产业研究主持人",
  member_version: 4,
  provider: "deepseek",
  model: "deepseek-chat",
};

test("live director normalization preserves the persisted moderator attribution", () => {
  const decision = normalizeDirectorDecision({
    type: "director_decision",
    decision: {
      id: "decision_1",
      room_id: "room_1",
      round_id: "round_1",
      source: "ai",
      moderator_context: moderatorContext,
    },
  }, "fallback_room", "fallback_round");

  assert.deepEqual(decision.moderator_context, moderatorContext);
  assert.notEqual(decision.moderator_context, moderatorContext);
});

test("normalization accepts moderator attribution from an unpersisted stream event", () => {
  const decision = normalizeDirectorDecision({
    source: "user_mention",
    moderator_context: {
      ...moderatorContext,
      decision_authority: "user_direction",
      model_used: false,
    },
  }, "room_1", "round_1");

  assert.equal(decision.moderator_context.decision_authority, "user_direction");
  assert.equal(decision.moderator_context.model_used, false);
});

test("moderator attribution states whether the frozen route was actually called", () => {
  const modelDecision = directorModeratorAttribution(moderatorContext);
  const userDecision = directorModeratorAttribution({
    ...moderatorContext,
    decision_authority: "user_direction",
    model_used: false,
  });

  assert.equal(modelDecision.memberName, "投资委员会主持人");
  assert.equal(modelDecision.memberVersion, 4);
  assert.equal(modelDecision.provider, "deepseek");
  assert.equal(modelDecision.model, "deepseek-chat");
  assert.equal(modelDecision.authorityLabel, "主持模型实际选择");
  assert.equal(userDecision.authorityLabel, "用户指向，未调用主持模型");
});

test("legacy decisions are marked honestly and new source labels are explicit", () => {
  const expectedLegacy = {
    available: false,
    legacy: true,
    notice: "旧记录未保存主持身份与模型归因",
  };
  assert.deepEqual(directorModeratorAttribution(null), expectedLegacy);
  assert.deepEqual(directorModeratorAttribution({}), expectedLegacy);
  assert.equal(directorSourceLabel("rules_first"), "规则优先");
  assert.equal(directorSourceLabel("director_circuit_breaker"), "主持熔断回退");
  assert.equal(directorSourceLabel("provider_call_budget_reserve"), "调用预算保留");
  assert.equal(directorSourceLabel("user_mention"), "用户点名");
  assert.equal(directorSourceLabel("user_interjection"), "用户插话");
});

test("contradictory moderator authority fails closed instead of claiming an AI choice", () => {
  const contradictory = directorModeratorAttribution({
    ...moderatorContext,
    decision_authority: "service_policy",
    model_used: true,
  });

  assert.equal(contradictory.complete, false);
  assert.equal(contradictory.authorityLabel, "模型调用归因不完整");
  assert.equal(contradictory.notice, "主持归因字段不完整");
});
