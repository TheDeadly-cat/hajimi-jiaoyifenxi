export const DEFAULT_CALIBRATION_MINIMUM_SAMPLES = 20;

function nonNegativeInteger(value, fallback = 0) {
  const number = Number(value);
  if (!Number.isFinite(number) || number < 0) return fallback;
  return Math.floor(number);
}

function positiveInteger(value, fallback) {
  const number = Number(value);
  if (!Number.isFinite(number) || number <= 0) return fallback;
  return Math.ceil(number);
}

export function calibrationMetricGate(row, fallbackMinimum = DEFAULT_CALIBRATION_MINIMUM_SAMPLES) {
  const source = row && typeof row === "object" ? row : {};
  const minimumSamples = positiveInteger(source.minimum_samples, fallbackMinimum);
  const sampleCount = nonNegativeInteger(source.sample_count);
  const enoughSamples = sampleCount >= minimumSamples;
  const comparable = source.mixed_conditions !== true && source.descriptive_only !== true;
  const qualified = source.qualified === true && enoughSamples && comparable;

  let reason = "尚未通过统计门";
  if (!enoughSamples) reason = "样本不足";
  else if (!comparable) reason = "条件不可直接比较";

  return {
    sampleCount,
    minimumSamples,
    progressLabel: `${sampleCount} / ${minimumSamples}`,
    qualified,
    reason,
  };
}

export function canShowBrierScore(row, fallbackMinimum = DEFAULT_CALIBRATION_MINIMUM_SAMPLES) {
  const gate = calibrationMetricGate(row, fallbackMinimum);
  const confidenceSampleCount = nonNegativeInteger(row?.confidence_sample_count);
  return gate.qualified && confidenceSampleCount >= gate.minimumSamples;
}
