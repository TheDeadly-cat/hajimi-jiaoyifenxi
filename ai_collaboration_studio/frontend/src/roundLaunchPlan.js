import {
  normalizeProjectRoundFocusAuthorization,
  projectRoundFocusAuthorizationPayload,
} from "./projectRoundFocus.js";
import {
  buildRoundContextAuthorizationSet,
  normalizeRoundContextAuthorizationSet,
} from "./roundContexts.js";

export const PROVIDER_CALL_LIMIT_MIN = 1;
export const PROVIDER_CALL_LIMIT_MAX = 28;

const ROUND_LAUNCH_PLAN_VERSION_V3 = "round_launch_plan_v3";
const ROUND_LAUNCH_PLAN_VERSION_V4 = "round_launch_plan_v4";
const ROUND_LAUNCH_PLAN_VERSION_V5 = "round_launch_plan_v5";
const PROVIDER_CALL_BUDGET_PROFILE_VERSION = "provider_call_budget_profile_v1";
const TURN_CONTRACT_VERSION = "turn_contract_v1";
const TURN_ENVELOPE_VERSION = "turn_envelope_v1";
const PROVIDER_OUTPUT_CAPABILITIES_VERSION = "provider_output_capabilities_v1";
const OUTPUT_MODES = new Set(["json_schema", "json_object", "prompt_json"]);
const PLAN_HASH_PATTERN = /^[0-9a-f]{64}$/;
const PROVIDER_ID_PATTERN = /^[a-z][a-z0-9_-]{0,39}$/;
const CLIENT_REQUEST_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const DISCUSSION_MODES = new Set(["dynamic", "sequential"]);
const MAX_MEMBERS = 200;
const MAX_ROUTES = 100;
const MAX_PROVIDERS = 50;
const MAX_BLOCKERS = 100;

function record(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function text(value, maximum = 240) {
  return typeof value === "string" ? value.trim().slice(0, maximum) : "";
}

function integer(value, fallback = 0, minimum = 0, maximum = 10000) {
  return Number.isInteger(value) && value >= minimum && value <= maximum
    ? value
    : fallback;
}

function providerId(value) {
  const normalized = text(value, 40).toLowerCase();
  return PROVIDER_ID_PATTERN.test(normalized) ? normalized : "";
}

function uniqueStrings(values, normalizer, maximum = 200) {
  if (!Array.isArray(values)) return [];
  const result = [];
  const seen = new Set();
  for (const rawValue of values.slice(0, maximum)) {
    const value = normalizer(rawValue);
    if (!value || seen.has(value)) continue;
    seen.add(value);
    result.push(value);
  }
  return result;
}

function normalizeMembers(value, errors) {
  if (!Array.isArray(value) || value.length === 0) {
    errors.push("成员列表缺失。");
    return [];
  }
  if (value.length > MAX_MEMBERS) errors.push("成员列表超过前端安全上限。");
  const seen = new Set();
  return value.slice(0, MAX_MEMBERS).flatMap((rawMember) => {
    const member = record(rawMember);
    const id = text(member.id, 160);
    const name = text(member.name, 160);
    const provider = providerId(member.provider);
    if (!id || !name || !provider || seen.has(id)) {
      errors.push("成员身份或路由字段无效。");
      return [];
    }
    seen.add(id);
    return [{
      id,
      version: integer(member.version, 1, 1, 1_000_000_000),
      name,
      identity: text(member.identity, 500),
      stage: text(member.stage, 80).toLowerCase() || "flexible",
      provider,
      model: text(member.model, 240),
    }];
  });
}

function normalizeRoutes(value, memberIds, errors) {
  if (!Array.isArray(value)) {
    errors.push("Provider 路由列表缺失。");
    return [];
  }
  if (value.length > MAX_ROUTES) errors.push("Provider 路由超过前端安全上限。");
  return value.slice(0, MAX_ROUTES).flatMap((rawRoute) => {
    const route = record(rawRoute);
    const provider = providerId(route.provider);
    const model = text(route.model, 240);
    const projectedCalls = integer(route.projected_preflight_calls, -1, 0, PROVIDER_CALL_LIMIT_MAX);
    const providerOutputModes = uniqueStrings(
      route.provider_output_modes,
      (item) => {
        const mode = text(item, 40).toLowerCase();
        return OUTPUT_MODES.has(mode) ? mode : "";
      },
      3,
    );
    const turnOutputMode = text(route.turn_output_mode, 40).toLowerCase();
    if (!provider || projectedCalls < 0) {
      errors.push("Provider 路由字段无效。");
      return [];
    }
    const routeMemberIds = uniqueStrings(route.member_ids, (item) => text(item, 160));
    if (routeMemberIds.some((id) => !memberIds.has(id))) {
      errors.push("Provider 路由引用了未知成员。");
    }
    const callable = route.callable === true;
    if ((callable && projectedCalls !== 1) || (!callable && projectedCalls !== 0)) {
      errors.push("Provider 路由调用投影与可调用状态不一致。");
    }
    if (
      route.output_capabilities_version !== PROVIDER_OUTPUT_CAPABILITIES_VERSION
      || providerOutputModes.length === 0
      || !providerOutputModes.includes(turnOutputMode)
      || typeof route.output_capabilities_declared !== "boolean"
    ) errors.push("Provider 结构化输出能力无法验证。");
    return [{
      provider,
      model,
      member_ids: routeMemberIds,
      known: route.known === true,
      configured: route.configured === true,
      policy_disabled: route.policy_disabled === true,
      skipped: route.skipped === true,
      callable,
      projected_preflight_calls: projectedCalls,
      output_capabilities_version: text(route.output_capabilities_version, 80),
      provider_output_modes: providerOutputModes,
      turn_output_mode: turnOutputMode,
      output_capabilities_declared: route.output_capabilities_declared === true,
    }];
  });
}

function normalizeCount(rawCalls, field, errors, maximum = 10000) {
  const rawValue = rawCalls[field];
  const value = integer(rawValue, -1, 0, maximum);
  if (value < 0) {
    errors.push(`调用次数字段 ${field} 无效。`);
    return 0;
  }
  return value;
}

function normalizeRange(value, field, errors) {
  const rawRange = record(value);
  const minimum = integer(rawRange.minimum, -1, 0, 10000);
  const maximum = integer(rawRange.maximum, -1, 0, 10000);
  if (minimum < 0 || maximum < minimum) {
    errors.push(`调用次数区间 ${field} 无效。`);
    return { minimum: 0, maximum: 0 };
  }
  return { minimum, maximum };
}

function normalizeCalls(value, errors) {
  const calls = record(value);
  if (
    calls.version !== PROVIDER_CALL_BUDGET_PROFILE_VERSION
    || calls.unit !== "provider_call_count"
    || calls.is_cost_estimate !== false
    || calls.is_usage_forecast !== false
    || calls.completion_assumes_valid_responses !== true
  ) {
    errors.push("调用次数口径无法验证。");
  }
  const normalized = {
    version: text(calls.version, 80),
    unit: text(calls.unit, 80),
    is_cost_estimate: calls.is_cost_estimate === true,
    is_usage_forecast: calls.is_usage_forecast === true,
    completion_assumes_valid_responses: calls.completion_assumes_valid_responses === true,
    unique_preflight_route_count: normalizeCount(calls, "unique_preflight_route_count", errors),
    projected_preflight_calls: normalizeCount(calls, "projected_preflight_calls", errors),
    workflow_minimum_speaker_calls: normalizeCount(calls, "workflow_minimum_speaker_calls", errors),
    minimum_speaker_calls: normalizeCount(calls, "minimum_speaker_calls", errors),
    minimum_director_calls: normalizeCount(calls, "minimum_director_calls", errors),
    recommended_director_calls: normalizeCount(calls, "recommended_director_calls", errors),
    optional_artifact_calls: normalizeCount(calls, "optional_artifact_calls", errors),
    contingency_calls: normalizeCount(calls, "contingency_calls", errors),
    recommended_provider_calls: normalizeCount(calls, "recommended_provider_calls", errors),
    recommended_authorization_calls: normalizeCount(calls, "recommended_authorization_calls", errors),
    projected_provider_calls_total: normalizeCount(
      calls,
      "projected_provider_calls_total",
      errors,
    ),
    maximum_speaker_calls: normalizeCount(calls, "maximum_speaker_calls", errors),
    maximum_director_calls: normalizeCount(calls, "maximum_director_calls", errors),
    initial_dispatch_provider_calls: normalizeCount(calls, "initial_dispatch_provider_calls", errors),
    runtime_rules_first_can_reduce_calls: calls.runtime_rules_first_can_reduce_calls === true,
    core_success_path_calls: normalizeCount(calls, "core_success_path_calls", errors),
    formal_path_call_ceiling_with_allowance: normalizeCount(
      calls,
      "formal_path_call_ceiling_with_allowance",
      errors,
    ),
    formal_path_conservative_upper_bound: normalizeCount(
      calls,
      "formal_path_conservative_upper_bound",
      errors,
    ),
    unprojected_call_kinds: uniqueStrings(
      calls.unprojected_call_kinds,
      (item) => text(item, 80).toLowerCase(),
      20,
    ),
    absolute_ceiling_source: text(calls.absolute_ceiling_source, 120),
    discussion_call_range: normalizeRange(calls.discussion_call_range, "discussion_call_range", errors),
    total_call_range: normalizeRange(calls.total_call_range, "total_call_range", errors),
  };
  if (normalized.total_call_range.minimum !== normalized.recommended_provider_calls) {
    errors.push("推荐调用次数与总调用区间不一致。");
  }
  if (
    normalized.minimum_director_calls !== 0
    || normalized.initial_dispatch_provider_calls !== 0
    || !normalized.runtime_rules_first_can_reduce_calls
    || normalized.recommended_authorization_calls !== normalized.recommended_provider_calls
    || normalized.core_success_path_calls
      !== normalized.projected_preflight_calls + normalized.minimum_speaker_calls
    || normalized.formal_path_call_ceiling_with_allowance
      !== normalized.projected_preflight_calls
        + normalized.maximum_speaker_calls
        + normalized.recommended_director_calls
        + normalized.optional_artifact_calls
    || normalized.formal_path_conservative_upper_bound
      !== normalized.projected_preflight_calls
        + normalized.maximum_speaker_calls
        + normalized.maximum_director_calls
        + normalized.optional_artifact_calls
    || normalized.total_call_range.maximum
      !== normalized.formal_path_conservative_upper_bound
    || normalized.discussion_call_range.minimum !== normalized.minimum_speaker_calls
    || normalized.discussion_call_range.maximum
      !== normalized.maximum_speaker_calls + normalized.maximum_director_calls
    || !normalized.unprojected_call_kinds.includes("round_interjection")
    || normalized.absolute_ceiling_source !== "user_authorized_max_provider_calls"
  ) errors.push("建议授权、歧义主持与结构上界无法验证。");
  return normalized;
}

function normalizeSafety(value, errors) {
  const safety = record(value);
  const verified = safety.budget_unit === "provider_call_count"
    && safety.is_cost_estimate === false
    && safety.execution_capability === "none"
    && safety.live_trading_allowed === false
    && safety.user_confirmation_required === true;
  if (!verified) errors.push("只读研究与无执行能力边界无法验证。");
  return {
    budget_unit: text(safety.budget_unit, 80),
    is_cost_estimate: safety.is_cost_estimate === true,
    execution_capability: text(safety.execution_capability, 80) || "unknown",
    live_trading_allowed: safety.live_trading_allowed === true,
    user_confirmation_required: safety.user_confirmation_required === true,
    verified,
  };
}

function normalizeBlockers(value, errors) {
  if (!Array.isArray(value)) {
    errors.push("阻断项列表缺失。");
    return [];
  }
  if (value.length > MAX_BLOCKERS) errors.push("阻断项超过前端安全上限。");
  return value.slice(0, MAX_BLOCKERS).map((rawBlocker) => {
    const blocker = record(rawBlocker);
    return {
      code: text(blocker.code, 120).toUpperCase() || "UNKNOWN_BLOCKER",
      provider: providerId(blocker.provider),
      model: text(blocker.model, 240),
      member_id: text(blocker.member_id, 160),
    };
  });
}

function normalizeProviderCallProjection(value, errors) {
  if (!Array.isArray(value)) {
    errors.push("Provider 完整调用投影缺失。");
    return [];
  }
  if (value.length > MAX_PROVIDERS) errors.push("Provider 调用投影超过前端安全上限。");
  const seen = new Set();
  const projection = value.slice(0, MAX_PROVIDERS).flatMap((rawProjection) => {
    const item = record(rawProjection);
    const provider = providerId(item.provider);
    if (!provider || seen.has(provider)) {
      errors.push("Provider 调用投影包含无效或重复标识。");
      return [];
    }
    seen.add(provider);
    const normalized = {
      provider,
      known: item.known === true,
      configured: item.configured === true,
      policy_disabled: item.policy_disabled === true,
      skipped: item.skipped === true,
      projected_preflight_calls: normalizeCount(item, "projected_preflight_calls", errors),
      minimum_speaker_calls: normalizeCount(item, "minimum_speaker_calls", errors),
      minimum_director_calls: normalizeCount(item, "minimum_director_calls", errors),
      recommended_director_calls: normalizeCount(item, "recommended_director_calls", errors),
      optional_artifact_calls: normalizeCount(item, "optional_artifact_calls", errors),
      contingency_calls: normalizeCount(item, "contingency_calls", errors),
      projected_provider_calls: normalizeCount(item, "projected_provider_calls", errors),
    };
    const breakdownTotal = normalized.projected_preflight_calls
      + normalized.minimum_speaker_calls
      + normalized.minimum_director_calls
      + normalized.recommended_director_calls
      + normalized.optional_artifact_calls
      + normalized.contingency_calls;
    if (breakdownTotal !== normalized.projected_provider_calls) {
      errors.push(`Provider ${provider} 的调用明细与合计不一致。`);
    }
    return [normalized];
  });
  if (!seen.has("openai")) {
    errors.push("Provider 调用投影缺少 OpenAI 0 次边界行。");
    projection.push({
      provider: "openai",
      known: false,
      configured: false,
      policy_disabled: true,
      skipped: true,
      projected_preflight_calls: 0,
      minimum_speaker_calls: 0,
      minimum_director_calls: 0,
      recommended_director_calls: 0,
      optional_artifact_calls: 0,
      contingency_calls: 0,
      projected_provider_calls: 0,
    });
  }
  return projection.sort((left, right) => left.provider.localeCompare(right.provider));
}

export function normalizeRoundLaunchPlan(value) {
  const source = record(value);
  const errors = [];
  const version = text(source.version, 80);
  if (![
    ROUND_LAUNCH_PLAN_VERSION_V3,
    ROUND_LAUNCH_PLAN_VERSION_V4,
    ROUND_LAUNCH_PLAN_VERSION_V5,
  ].includes(version)) {
    errors.push("启动确认单版本不受支持。");
  }
  const rawPlanHash = text(source.plan_hash, 64);
  const planHash = rawPlanHash.toLowerCase();
  if (!PLAN_HASH_PATTERN.test(planHash) || rawPlanHash !== planHash) {
    errors.push("计划哈希无效。");
  }
  const objective = text(source.objective, 4000);
  if (!objective) errors.push("本轮目标缺失。");

  const rawRoom = record(source.room);
  const roomId = text(rawRoom.id, 160);
  if (!roomId) errors.push("房间标识缺失。");
  const discussionMode = text(rawRoom.discussion_mode, 40).toLowerCase();
  if (!DISCUSSION_MODES.has(discussionMode)) {
    errors.push("讨论模式无效。");
  }
  const rawProtocols = record(rawRoom.protocols);
  const protocols = {
    turn_contract_version: text(rawProtocols.turn_contract_version, 80),
    turn_contract_required: rawProtocols.turn_contract_required === true,
    turn_envelope_version: text(rawProtocols.turn_envelope_version, 80),
    turn_envelope_schema_sha256: text(
      rawProtocols.turn_envelope_schema_sha256,
      64,
    ).toLowerCase(),
    provider_output_capabilities_version: text(
      rawProtocols.provider_output_capabilities_version,
      80,
    ),
    candidate_risk_review_version: rawProtocols.candidate_risk_review_version == null
      ? null
      : text(rawProtocols.candidate_risk_review_version, 80),
    candidate_risk_review_required: rawProtocols.candidate_risk_review_required === true,
  };
  if (
    protocols.turn_contract_version !== TURN_CONTRACT_VERSION
    || protocols.turn_contract_required !== true
    || protocols.turn_envelope_version !== TURN_ENVELOPE_VERSION
    || !PLAN_HASH_PATTERN.test(protocols.turn_envelope_schema_sha256)
    || protocols.provider_output_capabilities_version !== PROVIDER_OUTPUT_CAPABILITIES_VERSION
    || (protocols.candidate_risk_review_required
      ? !protocols.candidate_risk_review_version
      : protocols.candidate_risk_review_version !== null)
  ) errors.push("启动确认单的发言协议无法验证。");
  const pluginRegistrySnapshotSha256 = text(
    rawRoom.plugin_registry_snapshot_sha256,
    64,
  ).toLowerCase();
  if (pluginRegistrySnapshotSha256 && !PLAN_HASH_PATTERN.test(pluginRegistrySnapshotSha256)) {
    errors.push("启动确认单的插件合同封印无法验证。");
  }
  const room = {
    id: roomId,
    settings_version: integer(rawRoom.settings_version, 1, 1, 1_000_000_000),
    discussion_mode: discussionMode,
    domain: text(rawRoom.domain, 160),
    template_id: text(rawRoom.template_id, 160),
    capability_pack_ids: uniqueStrings(
      rawRoom.capability_pack_ids,
      (item) => text(item, 160),
    ),
    plugin_registry_snapshot_sha256: pluginRegistrySnapshotSha256,
    protocols,
  };
  const focusPackSelected = room.capability_pack_ids.includes("project_round_focus");
  const footballPackSelected = room.capability_pack_ids.includes("football_research_readonly");
  const stockPackSelected = room.capability_pack_ids.includes("stock_research_readonly");
  const expectedContextKeys = [
    ...(focusPackSelected ? [["project_round_focus", "core.round.context/v1"]] : []),
    ...(footballPackSelected
      ? [["football_research_readonly", "core.football.match_context/v1"]]
      : []),
    ...(stockPackSelected
      ? [["stock_research_readonly", "core.market.readonly_context/v1"]]
      : []),
  ].sort((left, right) => (
    left[0].localeCompare(right[0]) || left[1].localeCompare(right[1])
  ));
  const contextPackSelected = expectedContextKeys.length > 0;
  const rawFocusAuthorization = source.project_round_focus_authorization;
  const normalizedFocusAuthorization = normalizeProjectRoundFocusAuthorization(rawFocusAuthorization);
  const focusAuthorization = projectRoundFocusAuthorizationPayload(rawFocusAuthorization);
  const normalizedContextAuthorization = normalizeRoundContextAuthorizationSet(
    source.round_context_authorizations,
  );
  const contextAuthorization = normalizedContextAuthorization.valid
    ? normalizedContextAuthorization.value
    : null;
  const actualContextKeys = (contextAuthorization?.contexts || []).map((entry) => [
    entry.owner_pack_id,
    entry.port_id,
  ]);
  const contextKeysMatch = actualContextKeys.length === expectedContextKeys.length
    && actualContextKeys.every((key, index) => (
      key[0] === expectedContextKeys[index][0]
      && key[1] === expectedContextKeys[index][1]
    ));
  const legacyV4Valid = version === ROUND_LAUNCH_PLAN_VERSION_V4
    && focusPackSelected
    && !footballPackSelected
    && !stockPackSelected
    && normalizedFocusAuthorization.valid
    && Boolean(focusAuthorization)
    && !Object.hasOwn(source, "round_context_authorizations");
  const v5Valid = version === ROUND_LAUNCH_PLAN_VERSION_V5
    && contextPackSelected
    && normalizedContextAuthorization.valid
    && contextKeysMatch
    && !Object.hasOwn(source, "project_round_focus_authorization");
  const v3Valid = version === ROUND_LAUNCH_PLAN_VERSION_V3
    && !contextPackSelected
    && !Object.hasOwn(source, "project_round_focus_authorization")
    && !Object.hasOwn(source, "round_context_authorizations");
  if (
    !(legacyV4Valid || v5Valid || v3Valid)
    || (contextPackSelected && !pluginRegistrySnapshotSha256)
  ) errors.push("下一轮项目焦点授权与启动确认单版本不一致。");

  const members = normalizeMembers(source.members, errors);
  const memberIds = new Set(members.map((member) => member.id));
  const rawModerator = record(source.moderator);
  const moderator = {
    id: text(rawModerator.id, 160),
    version: integer(rawModerator.version, 1, 1, 1_000_000_000),
    name: text(rawModerator.name, 160),
    identity: text(rawModerator.identity, 500),
    stage: text(rawModerator.stage, 80).toLowerCase() || "flexible",
    provider: providerId(rawModerator.provider),
    model: text(rawModerator.model, 240),
    selection_source: text(rawModerator.selection_source, 120),
  };
  if (!moderator.id || !memberIds.has(moderator.id) || !moderator.provider) {
    errors.push("主持人不在当前成员快照中或其路由无效。");
  }

  const routes = normalizeRoutes(source.preflight_routes, memberIds, errors);
  const calls = normalizeCalls(source.calls, errors);
  const routeCallTotal = routes.reduce(
    (sum, route) => sum + route.projected_preflight_calls,
    0,
  );
  if (routeCallTotal !== calls.projected_preflight_calls) {
    errors.push("各 Provider 预检调用次数与汇总不一致。");
  }
  if (routes.length !== calls.unique_preflight_route_count) {
    errors.push("Provider 路由数量与汇总不一致。");
  }
  const providerCallProjection = normalizeProviderCallProjection(
    source.provider_call_projection,
    errors,
  );
  const providerProjectionTotal = providerCallProjection.reduce(
    (sum, item) => sum + item.projected_provider_calls,
    0,
  );
  if (
    providerProjectionTotal !== calls.projected_provider_calls_total
    || providerProjectionTotal !== calls.recommended_provider_calls
  ) errors.push("各 Provider 完整调用投影与推荐总数不一致。");

  const rawSkipProviderIds = source.skip_provider_ids;
  if (!Array.isArray(rawSkipProviderIds) || rawSkipProviderIds.length > 20) {
    errors.push("跳过 Provider 列表无效。");
  }
  const skipProviderIds = uniqueStrings(rawSkipProviderIds, providerId, 20).sort();
  if (
    Array.isArray(rawSkipProviderIds)
    && rawSkipProviderIds.slice(0, 20).some((item) => !providerId(item))
  ) errors.push("跳过 Provider 列表包含无效标识。");
  const safety = normalizeSafety(source.safety, errors);
  const blockers = normalizeBlockers(source.blockers, errors);
  if (source.ready_for_authorization !== true && blockers.length === 0) {
    blockers.push({ code: "PLAN_NOT_READY", provider: "", model: "", member_id: "" });
  }
  if (errors.length) {
    blockers.push({
      code: "CLIENT_PLAN_INVALID",
      provider: "",
      model: "",
      member_id: "",
    });
  }

  return {
    version,
    plan_hash: planHash,
    objective,
    room,
    members,
    moderator,
    skip_provider_ids: skipProviderIds,
    preflight_routes: routes,
    provider_call_projection: providerCallProjection,
    calls,
    safety,
    project_round_focus_authorization: legacyV4Valid ? focusAuthorization : null,
    round_context_authorizations: v5Valid ? contextAuthorization : null,
    ready_for_authorization: source.ready_for_authorization === true
      && blockers.length === 0
      && errors.length === 0,
    blockers,
    normalization_errors: errors,
    valid: errors.length === 0,
  };
}

export function isValidClientRoundRequestId(value) {
  return typeof value === "string" && CLIENT_REQUEST_ID_PATTERN.test(value.trim());
}

export function createRoundLaunchRequestContext(
  {
    roomId,
    roomSettingsVersion,
    objective,
    projectRoundFocusAuthorization = null,
    roundContextAuthorizations = null,
  } = {},
  { previous = null, createRequestId } = {},
) {
  const cleanRoomId = text(roomId, 160);
  const cleanObjective = text(objective, 4000);
  const cleanSettingsVersion = integer(roomSettingsVersion, -1, 1, 1_000_000_000);
  if (!cleanRoomId || !cleanObjective || cleanSettingsVersion < 1) {
    throw new TypeError("房间、版本或本轮目标无效。");
  }
  const focusAuthorization = projectRoundFocusAuthorization == null
    ? null
    : projectRoundFocusAuthorizationPayload(projectRoundFocusAuthorization);
  if (projectRoundFocusAuthorization != null && !focusAuthorization) {
    throw new TypeError("下一轮项目焦点授权无效。");
  }
  if (projectRoundFocusAuthorization != null && roundContextAuthorizations != null) {
    throw new TypeError("Generic and legacy round context authorizations cannot be combined.");
  }
  const contextAuthorizationState = roundContextAuthorizations == null
    ? null
    : normalizeRoundContextAuthorizationSet(roundContextAuthorizations);
  if (contextAuthorizationState && !contextAuthorizationState.valid) {
    throw new TypeError("Round context authorizations are invalid.");
  }
  const contextAuthorizations = contextAuthorizationState?.value || null;
  const reusable = record(previous);
  const reusableFocusAuthorization = projectRoundFocusAuthorizationPayload(
    reusable.projectRoundFocusAuthorization,
  );
  const focusSemantics = JSON.stringify(focusAuthorization);
  const reusableContextState = reusable.roundContextAuthorizations == null
    ? null
    : normalizeRoundContextAuthorizationSet(reusable.roundContextAuthorizations);
  const canReuse = reusable.roomId === cleanRoomId
    && reusable.roomSettingsVersion === cleanSettingsVersion
    && reusable.objective === cleanObjective
    && JSON.stringify(reusableFocusAuthorization) === focusSemantics
    && JSON.stringify(reusableContextState?.value || null)
      === JSON.stringify(contextAuthorizations)
    && isValidClientRoundRequestId(reusable.clientRoundRequestId);
  const clientRoundRequestId = canReuse
    ? reusable.clientRoundRequestId
    : typeof createRequestId === "function" ? createRequestId() : "";
  if (!isValidClientRoundRequestId(clientRoundRequestId)) {
    throw new TypeError("无法创建 client_round_request_id。");
  }
  return {
    roomId: cleanRoomId,
    roomSettingsVersion: cleanSettingsVersion,
    objective: cleanObjective,
    projectRoundFocusAuthorization: focusAuthorization,
    roundContextAuthorizations: contextAuthorizations,
    clientRoundRequestId: clientRoundRequestId.trim(),
  };
}

export function roundLaunchPlanContextState(
  plan,
  {
    roomId,
    roomSettingsVersion,
    objective,
    skipProviders = [],
    projectRoundFocusAuthorization = null,
    roundContextAuthorizations = null,
  } = {},
) {
  const normalized = normalizeRoundLaunchPlan(plan);
  const expectedRoomId = text(roomId, 160);
  const expectedObjective = text(objective, 4000);
  const expectedSettingsVersion = integer(roomSettingsVersion, -1, 1, 1_000_000_000);
  const expectedSkipProviders = uniqueStrings(skipProviders, providerId, 20).sort();
  const expectedFocusAuthorization = projectRoundFocusAuthorization == null
    ? null
    : projectRoundFocusAuthorizationPayload(projectRoundFocusAuthorization);
  const expectedContextState = roundContextAuthorizations == null
    ? null
    : normalizeRoundContextAuthorizationSet(roundContextAuthorizations);
  const expectedContextAuthorizations = expectedContextState?.valid
    ? expectedContextState.value
    : null;
  let error = "";
  if (projectRoundFocusAuthorization != null && roundContextAuthorizations != null) {
    error = "Generic and legacy round context authorizations cannot be combined.";
  } else if (!normalized.valid) {
    error = "启动确认单无法安全读取，请重新生成。";
  } else if (normalized.room.id !== expectedRoomId) {
    error = "房间已切换，原启动确认单已失效。";
  } else if (normalized.room.settings_version !== expectedSettingsVersion) {
    error = "房间配置版本已变化，请重新生成启动确认单。";
  } else if (normalized.objective !== expectedObjective) {
    error = "本轮目标已变化，请重新生成启动确认单。";
  } else if (
    normalized.skip_provider_ids.length !== expectedSkipProviders.length
    || normalized.skip_provider_ids.some((item, index) => item !== expectedSkipProviders[index])
  ) {
    error = "Provider 跳过策略已变化，请重新生成启动确认单。";
  } else if (
    !expectedContextState?.valid && roundContextAuthorizations != null
  ) {
    error = "Round context authorizations are invalid.";
  } else if (
    JSON.stringify(normalized.round_context_authorizations)
      !== JSON.stringify(expectedContextAuthorizations)
  ) {
    error = "Round context authorization sources changed; rebuild the launch plan.";
  } else if (
    JSON.stringify(normalized.project_round_focus_authorization)
      !== JSON.stringify(expectedFocusAuthorization)
  ) {
    error = "下一轮项目焦点来源或预览封印已变化，请重新读取焦点与启动确认单。";
  }
  return {
    plan: normalized,
    matches: !error,
    error,
  };
}

export function roundLaunchAuthorizationState(plan, maxProviderCalls) {
  const normalized = normalizeRoundLaunchPlan(plan);
  const validLimit = Number.isInteger(maxProviderCalls)
    && maxProviderCalls >= PROVIDER_CALL_LIMIT_MIN
    && maxProviderCalls <= PROVIDER_CALL_LIMIT_MAX;
  const recommended = normalized.calls.recommended_provider_calls;
  const belowRecommended = validLimit && maxProviderCalls < recommended;
  return {
    plan: normalized,
    maxProviderCalls: validLimit ? maxProviderCalls : null,
    recommendedProviderCalls: recommended,
    validLimit,
    belowRecommended,
    canConfirm: validLimit && normalized.ready_for_authorization,
    error: validLimit ? "" : `Provider 调用次数上限必须是 ${PROVIDER_CALL_LIMIT_MIN} 到 ${PROVIDER_CALL_LIMIT_MAX} 之间的整数。`,
    warning: belowRecommended
      ? {
        code: "BELOW_RECOMMENDED_PROVIDER_CALLS",
        message: `当前上限低于推荐的 ${recommended} 次；仍可确认，但讨论可能提前停止。`,
      }
      : null,
  };
}

export function buildRoundLaunchAuthorizationPayload(
  plan,
  { clientRoundRequestId, maxProviderCalls } = {},
) {
  const authorization = roundLaunchAuthorizationState(plan, maxProviderCalls);
  if (!authorization.validLimit) throw new TypeError(authorization.error);
  if (!authorization.plan.ready_for_authorization) {
    throw new Error("启动确认单存在阻断项，当前不能确认。");
  }
  if (!isValidClientRoundRequestId(clientRoundRequestId)) {
    throw new TypeError("client_round_request_id 无效。");
  }
  return {
    client_round_request_id: clientRoundRequestId.trim(),
    plan_hash: authorization.plan.plan_hash,
    max_provider_calls: maxProviderCalls,
    objective: authorization.plan.objective,
    skip_providers: [...authorization.plan.skip_provider_ids],
    ...(authorization.plan.project_round_focus_authorization
      ? {
        project_round_focus_authorization: projectRoundFocusAuthorizationPayload(
          authorization.plan.project_round_focus_authorization,
        ),
      }
      : {}),
    ...(authorization.plan.round_context_authorizations
      ? {
        round_context_authorizations: buildRoundContextAuthorizationSet(
          authorization.plan.round_context_authorizations.contexts,
        ),
      }
      : {}),
  };
}
