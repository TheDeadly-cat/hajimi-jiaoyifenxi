import assert from "node:assert/strict";
import test from "node:test";

import { walkForwardIntegrityState } from "../src/walkForwardIntegrity.js";


test("shows metrics only for a fully verified v2 record", () => {
  const state = walkForwardIntegrityState({
    integrity_ok: true,
    fully_verified: true,
    integrity_status: "verified",
    integrity_issues: [],
  });

  assert.equal(state.metricsVisible, true);
  assert.equal(state.label, "完整性已验证");
});


test("fails closed when integrity fields are missing or failed", () => {
  assert.equal(walkForwardIntegrityState({}).metricsVisible, false);
  const state = walkForwardIntegrityState({
    integrity_ok: false,
    fully_verified: false,
    integrity_status: "failed",
    integrity_issues: ["WALK_FORWARD_RESULT_HASH_MISMATCH"],
  });

  assert.equal(state.metricsVisible, false);
  assert.equal(state.label, "完整性校验失败");
  assert.match(state.detail, /指标已隐藏/);
});


test("labels legacy v1 without a frozen input as unverifiable", () => {
  const state = walkForwardIntegrityState({
    integrity_ok: false,
    fully_verified: false,
    integrity_status: "legacy_unverifiable",
    integrity_issues: ["WALK_FORWARD_INPUT_SNAPSHOT_LEGACY_UNVERIFIABLE"],
  });

  assert.equal(state.metricsVisible, false);
  assert.equal(state.label, "旧格式未完全可验");
  assert.match(state.detail, /v1/);
});
