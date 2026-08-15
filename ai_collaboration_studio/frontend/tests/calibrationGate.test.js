import test from "node:test";
import assert from "node:assert/strict";
import {
  calibrationMetricGate,
  canShowBrierScore,
} from "../src/calibrationGate.js";

test("hides hit-rate and calibration metrics below twenty samples", () => {
  const row = {
    sample_count: 19,
    confidence_sample_count: 19,
    minimum_samples: 20,
    qualified: false,
    hit_rate_pct: 84.21,
    brier_score: 0.12,
    calibration_gap_pp: 9.5,
  };

  assert.deepEqual(calibrationMetricGate(row), {
    sampleCount: 19,
    minimumSamples: 20,
    progressLabel: "19 / 20",
    qualified: false,
    reason: "样本不足",
  });
  assert.equal(canShowBrierScore(row), false);
});

test("fails closed when a contradictory qualified flag has too few samples", () => {
  const gate = calibrationMetricGate({
    sample_count: 1,
    minimum_samples: 20,
    qualified: true,
  });

  assert.equal(gate.qualified, false);
  assert.equal(gate.progressLabel, "1 / 20");
});

test("does not publish mixed-condition rates even after the count reaches the threshold", () => {
  const gate = calibrationMetricGate({
    sample_count: 20,
    minimum_samples: 20,
    qualified: false,
    mixed_conditions: true,
  });

  assert.equal(gate.qualified, false);
  assert.equal(gate.reason, "条件不可直接比较");
});

test("publishes qualified metrics only after the sample and confidence gates both pass", () => {
  const row = {
    sample_count: 20,
    confidence_sample_count: 20,
    minimum_samples: 20,
    qualified: true,
  };

  assert.equal(calibrationMetricGate(row).qualified, true);
  assert.equal(canShowBrierScore(row), true);
  assert.equal(canShowBrierScore({ ...row, confidence_sample_count: 1 }), false);
});
