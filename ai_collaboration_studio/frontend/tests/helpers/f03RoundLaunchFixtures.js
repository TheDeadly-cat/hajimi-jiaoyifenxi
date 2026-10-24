export function readyPlan(overrides = {}) {
  return {
    version: "round_launch_plan_v3",
    plan_hash: "a".repeat(64),
    objective: "F03 合成讨论：检查计划与用户确认",
    room: {
      id: "room_storage",
      settings_version: 4,
      discussion_mode: "dynamic",
      domain: "us_equities_research",
      template_id: "us_storage_committee",
      capability_pack_ids: ["us_equities_research"],
      protocols: {
        turn_contract_version: "turn_contract_v1",
        turn_contract_required: true,
        turn_envelope_version: "turn_envelope_v1",
        turn_envelope_schema_sha256: "b".repeat(64),
        provider_output_capabilities_version: "provider_output_capabilities_v1",
        candidate_risk_review_version: "candidate_risk_review_v1",
        candidate_risk_review_required: true,
      },
    },
    members: [
      {
        id: "member_moderator",
        version: 2,
        name: "研究主持人",
        identity: "控制讨论顺序与证据门槛",
        stage: "evidence_intake",
        provider: "deepseek",
        model: "deepseek-v4-pro",
      },
      {
        id: "member_risk",
        version: 1,
        name: "风险负责人",
        identity: "提出反证与停止条件",
        stage: "risk_review",
        provider: "doubao",
        model: "doubao-seed-test",
      },
    ],
    moderator: {
      id: "member_moderator",
      version: 2,
      name: "研究主持人",
      identity: "控制讨论顺序与证据门槛",
      stage: "evidence_intake",
      provider: "deepseek",
      model: "deepseek-v4-pro",
      selection_source: "configured",
    },
    skip_provider_ids: ["openai"],
    preflight_routes: [
      {
        provider: "deepseek",
        model: "deepseek-v4-pro",
        member_ids: ["member_moderator"],
        known: true,
        configured: true,
        policy_disabled: false,
        skipped: false,
        callable: true,
        projected_preflight_calls: 1,
        output_capabilities_version: "provider_output_capabilities_v1",
        provider_output_modes: ["json_object", "prompt_json"],
        turn_output_mode: "json_object",
        output_capabilities_declared: true,
      },
      {
        provider: "doubao",
        model: "doubao-seed-test",
        member_ids: ["member_risk"],
        known: true,
        configured: true,
        policy_disabled: false,
        skipped: false,
        callable: true,
        projected_preflight_calls: 1,
        output_capabilities_version: "provider_output_capabilities_v1",
        provider_output_modes: ["json_object", "prompt_json"],
        turn_output_mode: "json_object",
        output_capabilities_declared: true,
      },
    ],
    provider_call_projection: [
      {
        provider: "deepseek",
        known: true,
        configured: true,
        policy_disabled: false,
        skipped: false,
        projected_preflight_calls: 1,
        minimum_speaker_calls: 9,
        minimum_director_calls: 0,
        recommended_director_calls: 6,
        optional_artifact_calls: 0,
        contingency_calls: 0,
        projected_provider_calls: 16,
      },
      {
        provider: "doubao",
        known: true,
        configured: true,
        policy_disabled: false,
        skipped: false,
        projected_preflight_calls: 1,
        minimum_speaker_calls: 3,
        minimum_director_calls: 0,
        recommended_director_calls: 0,
        optional_artifact_calls: 1,
        contingency_calls: 0,
        projected_provider_calls: 5,
      },
      {
        provider: "openai",
        known: true,
        configured: true,
        policy_disabled: true,
        skipped: true,
        projected_preflight_calls: 0,
        minimum_speaker_calls: 0,
        minimum_director_calls: 0,
        recommended_director_calls: 0,
        optional_artifact_calls: 0,
        contingency_calls: 0,
        projected_provider_calls: 0,
      },
    ],
    calls: {
      version: "provider_call_budget_profile_v1",
      unit: "provider_call_count",
      is_cost_estimate: false,
      is_usage_forecast: false,
      completion_assumes_valid_responses: true,
      unique_preflight_route_count: 2,
      projected_preflight_calls: 2,
      workflow_minimum_speaker_calls: 12,
      minimum_speaker_calls: 12,
      minimum_director_calls: 0,
      recommended_director_calls: 6,
      optional_artifact_calls: 1,
      contingency_calls: 0,
      recommended_provider_calls: 21,
      recommended_authorization_calls: 21,
      projected_provider_calls_total: 21,
      maximum_speaker_calls: 18,
      maximum_director_calls: 17,
      initial_dispatch_provider_calls: 0,
      runtime_rules_first_can_reduce_calls: true,
      core_success_path_calls: 14,
      formal_path_call_ceiling_with_allowance: 27,
      formal_path_conservative_upper_bound: 38,
      unprojected_call_kinds: ["round_interjection"],
      absolute_ceiling_source: "user_authorized_max_provider_calls",
      discussion_call_range: { minimum: 12, maximum: 35 },
      total_call_range: { minimum: 21, maximum: 38 },
    },
    safety: {
      budget_unit: "provider_call_count",
      is_cost_estimate: false,
      execution_capability: "none",
      live_trading_allowed: false,
      user_confirmation_required: true,
    },
    ready_for_authorization: true,
    blockers: [],
    ...overrides,
  };
}

function suspendFirstRoute(plan) {
  plan.preflight_routes[0].projected_preflight_calls = 0;
  plan.provider_call_projection[0].projected_preflight_calls = 0;
  plan.provider_call_projection[0].projected_provider_calls -= 1;
  for (const field of ["projected_preflight_calls", "recommended_provider_calls", "recommended_authorization_calls", "projected_provider_calls_total", "core_success_path_calls", "formal_path_call_ceiling_with_allowance", "formal_path_conservative_upper_bound"]) plan.calls[field] -= 1;
  plan.calls.total_call_range.minimum -= 1;
  plan.calls.total_call_range.maximum -= 1;
}

export function f03Plans() {
  const blocked = (blockers) => readyPlan({ ready_for_authorization: false, blockers });
  const missing = blocked([
    { code: "WORKFLOW_PROVIDER_COVERAGE_INSUFFICIENT" },
    { code: "MODERATOR_PROVIDER_ROUTE_UNAVAILABLE" },
    { code: "PROVIDER_NOT_CONFIGURED", provider: "deepseek" },
  ]);
  missing.preflight_routes[0] = { ...missing.preflight_routes[0], configured: false, callable: false };
  missing.provider_call_projection[0].configured = false;
  suspendFirstRoute(missing);
  const disabled = blocked([{ code: "PROVIDER_POLICY_DISABLED", provider: "deepseek" }]);
  disabled.preflight_routes[0] = { ...disabled.preflight_routes[0], policy_disabled: true, callable: false };
  disabled.provider_call_projection[0].policy_disabled = true;
  suspendFirstRoute(disabled);
  return {
    ready: readyPlan(), missing, disabled,
    deploymentLimit: blocked([{ code: "RECOMMENDATION_EXCEEDS_DEPLOYMENT_HARD_LIMIT" }]),
    unknown: blocked([{ code: "F03_UNRECOGNIZED_BLOCKER" }]),
    loadingFailure: null,
  };
}
