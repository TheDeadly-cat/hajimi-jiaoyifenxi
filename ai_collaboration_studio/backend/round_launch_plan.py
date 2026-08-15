from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import Counter
from typing import Any

from .providers.output import (
    OUTPUT_CAPABILITIES_VERSION,
    OUTPUT_MODE_PRIORITY,
)
from .round_contexts import (
    DEFAULT_ROUND_CONTEXT_PROVIDERS,
    RoundContextError,
    coerce_round_context_authorization_set,
    normalize_round_context_prepared,
)
from .turn_envelope import (
    TURN_ENVELOPE_SCHEMA_SHA256,
    TURN_ENVELOPE_VERSION,
)
from .turn_contract import (
    CANDIDATE_RISK_REVIEW_VERSION,
    TURN_CONTRACT_VERSION,
    candidate_risk_review_protocol_required,
)
from .workflow_policy import clean_capabilities, validate_workflow_policy


ROUND_LAUNCH_PLAN_VERSION = "round_launch_plan_v3"
ROUND_LAUNCH_PLAN_VERSION_V4 = "round_launch_plan_v4"
ROUND_LAUNCH_PLAN_VERSION_V5 = "round_launch_plan_v5"
ROUND_LAUNCH_AUTHORIZATION_VERSION = "round_launch_authorization_v1"
PROVIDER_CALL_BUDGET_PROFILE_VERSION = "provider_call_budget_profile_v1"
DEFAULT_PROVIDER_CALL_HARD_LIMIT = 28

_PROVIDER_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
_PLAN_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _canonical_sha256(value: dict[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _positive_int(value: Any, *, field: str, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{field} must be an integer greater than or equal to {minimum}")
    return value


def validate_authorization(
    plan_hash: str,
    max_provider_calls: int,
    *,
    recommended_provider_calls: int,
    expected_plan_hash: str | None = None,
) -> dict[str, Any]:
    """Validate a user-supplied provider-call ceiling without estimating money.

    This pure helper deliberately requires the recommendation.  Most callers can
    instead use :meth:`RoundLaunchPlanService.validate_authorization`, which
    remembers recommendations for plans built by that service instance.
    """

    clean_hash = str(plan_hash or "").strip().lower()
    if not _PLAN_HASH_PATTERN.fullmatch(clean_hash):
        raise ValueError("plan_hash must be a lowercase SHA-256 digest")
    if expected_plan_hash is not None:
        clean_expected = str(expected_plan_hash or "").strip().lower()
        if clean_hash != clean_expected:
            raise ValueError("plan_hash does not match the frozen launch plan")

    call_limit = _positive_int(max_provider_calls, field="max_provider_calls")
    if call_limit > DEFAULT_PROVIDER_CALL_HARD_LIMIT:
        raise ValueError(
            "max_provider_calls exceeds the deployment hard limit of "
            f"{DEFAULT_PROVIDER_CALL_HARD_LIMIT}"
        )
    recommendation = _positive_int(
        recommended_provider_calls,
        field="recommended_provider_calls",
        minimum=0,
    )
    if recommendation > DEFAULT_PROVIDER_CALL_HARD_LIMIT:
        raise ValueError("the plan recommendation exceeds the deployment hard limit")

    sufficient = call_limit >= recommendation
    warning = None
    if not sufficient:
        warning = {
            "code": "BELOW_RECOMMENDED_PROVIDER_CALLS",
            "message": (
                "The authorization is valid but below the plan recommendation; "
                "round completion must not be described as sufficiently funded."
            ),
        }
    return {
        "version": ROUND_LAUNCH_AUTHORIZATION_VERSION,
        "plan_hash": clean_hash,
        "max_provider_calls": call_limit,
        "deployment_hard_limit": DEFAULT_PROVIDER_CALL_HARD_LIMIT,
        "recommended_provider_calls": recommendation,
        "valid": True,
        "sufficient": sufficient,
        "warning": warning,
    }


class RoundLaunchPlanService:
    """Build a deterministic, read-only plan before a discussion is launched.

    ``build`` only reads a room snapshot and the provider registry's local
    ``status()`` projection.  It never probes a provider, generates text, writes
    a round, or handles credentials.
    """

    def __init__(self, store: Any, providers: Any) -> None:
        self.store = store
        self.providers = providers
        self._recommendations_by_hash: dict[str, int] = {}

    def build(
        self,
        room_id: str,
        objective: Any,
        skip_provider_ids: set[str] | list[str] | tuple[str, ...] | None = None,
        project_round_focus_authorization: Any = None,
        *,
        round_context_authorizations: Any = None,
        configuration_only: bool = False,
    ) -> dict[str, Any]:
        clean_room_id = str(room_id or "").strip()
        if not clean_room_id:
            raise ValueError("room_id is required")
        clean_objective = self.store.clean_round_objective(objective)
        skip_ids = self._clean_skip_provider_ids(skip_provider_ids)
        if configuration_only:
            # Local Provider configuration inspection neither consumes nor
            # validates user confirmations for unrelated domain contexts.
            project_round_focus_authorization = None
            round_context_authorizations = None

        raw_snapshot = self.store.room_snapshot(clean_room_id)
        if not raw_snapshot:
            raise ValueError("room does not exist")
        # Do not retain references to mutable store/provider projections.
        snapshot = copy.deepcopy(raw_snapshot)
        room = snapshot.get("room")
        if not isinstance(room, dict):
            raise ValueError("room snapshot is invalid")
        if (
            "plugin_registry_integrity_ok" in room
            and room.get("plugin_registry_integrity_ok") is not True
        ):
            raise ValueError("room plugin registry snapshot is invalid")
        lifecycle_current = room.get("plugin_lifecycle_current")
        if isinstance(lifecycle_current, dict) and (
            lifecycle_current.get("integrity_ok") is not True
            or (
                not configuration_only
                and lifecycle_current.get("new_round_allowed") is not True
            )
        ):
            raise ValueError(
                "room plugin lifecycle does not allow a new formal round"
            )
        selected_round_context_keys: tuple[tuple[str, str], ...] = ()
        canonical_round_context_authorizations: dict[str, Any] | None = None
        prepared_round_contexts: dict[str, Any] | None = None
        if not configuration_only:
            registry_snapshot = room.get("plugin_registry_snapshot")
            active_pack_ids = {
                str(item or "").strip()
                for item in room.get("active_capability_pack_ids") or []
                if str(item or "").strip()
            }
            registered_context_pack_ids = {
                owner_pack_id
                for owner_pack_id, _port_id
                in DEFAULT_ROUND_CONTEXT_PROVIDERS.provider_keys()
            }
            required_context_pack_ids = (
                active_pack_ids & registered_context_pack_ids
            )
            if isinstance(registry_snapshot, dict):
                selected_round_context_keys = (
                    DEFAULT_ROUND_CONTEXT_PROVIDERS.selected_provider_keys(
                        registry_snapshot
                    )
                )
                if {
                    owner_pack_id
                    for owner_pack_id, _port_id in selected_round_context_keys
                } != required_context_pack_ids:
                    raise RoundContextError(
                        "Active round-context packs do not match the frozen registry.",
                        code="ROUND_CONTEXT_REGISTRY_INVALID",
                    )
            elif required_context_pack_ids:
                raise RoundContextError(
                    "The frozen plugin registry is required for round contexts.",
                    code="ROUND_CONTEXT_REGISTRY_INVALID",
                )

            canonical_set = coerce_round_context_authorization_set(
                round_context_authorizations,
                legacy_project_round_focus_authorization=(
                    project_round_focus_authorization
                ),
            )
            if selected_round_context_keys or canonical_set["contexts"]:
                prepared_round_contexts = (
                    DEFAULT_ROUND_CONTEXT_PROVIDERS.prepare_authorized_set(
                        self.store,
                        clean_room_id,
                        canonical_set,
                    )
                )
                prepared_keys = set(
                    normalize_round_context_prepared(prepared_round_contexts)
                )
                if prepared_keys != set(selected_round_context_keys):
                    raise RoundContextError(
                        "Prepared round contexts do not match the frozen registry.",
                        code="ROUND_CONTEXT_PROVIDER_OUTPUT_INVALID",
                    )
            if selected_round_context_keys:
                canonical_round_context_authorizations = canonical_set
        workflow_policy = validate_workflow_policy(room.get("workflow_policy"))

        statuses, provider_status_available = self._safe_provider_statuses()
        status_by_id = {item["id"]: item for item in statuses}
        policy_disabled_ids = {
            str(item or "").strip().lower()
            for item in getattr(self.providers, "disabled_provider_ids", frozenset())
            if _PROVIDER_ID_PATTERN.fullmatch(str(item or "").strip().lower())
        }

        raw_members = [
            member
            for member in (snapshot.get("members") or [])
            if isinstance(member, dict) and member.get("enabled") is True
        ]
        members = [
            self._freeze_member(member, status_by_id)
            for member in raw_members
        ]
        if not members:
            raise ValueError("room has no enabled members")

        moderator, moderator_source = self._freeze_moderator(
            room,
            members,
            workflow_policy,
        )
        routes = self._project_routes(
            members,
            status_by_id=status_by_id,
            policy_disabled_ids=policy_disabled_ids,
            skip_ids=set(skip_ids),
        )
        route_by_key = {
            (route["provider"], route["model"]): route
            for route in routes
        }
        callable_member_ids = {
            member["id"]
            for member in members
            if route_by_key[(member["provider"], member["model"])]["callable"]
        }
        callable_raw_members = [
            raw_member
            for raw_member, frozen_member in zip(raw_members, members, strict=True)
            if frozen_member["id"] in callable_member_ids
        ]

        discussion_mode = str(room.get("discussion_mode") or "dynamic").strip().lower()
        if discussion_mode not in {"dynamic", "sequential"}:
            raise ValueError("room discussion_mode is invalid")
        moderator_route = route_by_key[(moderator["provider"], moderator["model"])]
        dynamic_moderator_callable = (
            discussion_mode != "dynamic" or moderator_route["callable"]
        )

        workflow_policy_floor_calls = max(
            int(workflow_policy["minimum_successful_members"]),
            sum(int(value) for value in workflow_policy["minimum_stage_coverage"].values()),
        )
        minimum_speaker_member_ids = self._minimum_workflow_speaker_member_ids(
            callable_raw_members,
            workflow_policy,
        )
        workflow_minimum_speaker_calls = max(
            workflow_policy_floor_calls,
            len(minimum_speaker_member_ids or []),
        )
        workflow_ready = (
            minimum_speaker_member_ids is not None
            and dynamic_moderator_callable
        )

        follow_up_budget = (
            int(workflow_policy["follow_up_budget"])
            if discussion_mode == "dynamic" and len(callable_raw_members) > 1
            else 0
        )
        maximum_speaker_calls = min(
            len(callable_raw_members) * int(workflow_policy["max_turns_per_member"]),
            len(callable_raw_members) + follow_up_budget,
        )
        if maximum_speaker_calls < workflow_minimum_speaker_calls:
            workflow_ready = False

        minimum_speaker_calls = workflow_minimum_speaker_calls if workflow_ready else 0
        # The rules-first director does not call a Provider for the initial
        # dispatch or when one deterministic rule resolves the next speaker.
        # A model call is only possible between formal speaker attempts, so the
        # structural upper bound is Smax - 1 rather than Smax + 1.  Keep the
        # recommendation separate from both the true minimum (zero) and that
        # structural bound: it is an authorization allowance, not a usage
        # forecast or a completion guarantee.
        minimum_director_calls = 0
        maximum_director_calls = (
            max(0, maximum_speaker_calls - 1)
            if workflow_ready and discussion_mode == "dynamic"
            else 0
        )
        recommended_director_calls = (
            min(follow_up_budget, maximum_director_calls)
            if workflow_ready and discussion_mode == "dynamic"
            else 0
        )
        if not workflow_ready:
            maximum_speaker_calls = 0
        optional_artifact_calls = 1 if workflow_ready else 0
        projected_preflight_calls = sum(
            int(route["projected_preflight_calls"])
            for route in routes
        )
        minimum_discussion_calls = minimum_speaker_calls
        recommended_discussion_calls = (
            minimum_speaker_calls + recommended_director_calls
        )
        maximum_discussion_calls = maximum_speaker_calls + maximum_director_calls
        calculated_recommendation = (
            projected_preflight_calls
            + recommended_discussion_calls
            + optional_artifact_calls
        )
        # Compatibility field retained at zero for one protocol generation.
        # Unmodelled interjections share the user's absolute ledger ceiling and
        # must never be hidden inside an unexplained contingency bucket.
        contingency_calls = 0
        recommendation = calculated_recommendation + contingency_calls
        provider_call_projection = self._project_provider_calls(
            members=members,
            routes=routes,
            statuses=statuses,
            policy_disabled_ids=policy_disabled_ids,
            skip_ids=set(skip_ids),
            minimum_speaker_calls=minimum_speaker_calls,
            minimum_director_calls=minimum_director_calls,
            recommended_director_calls=recommended_director_calls,
            optional_artifact_calls=optional_artifact_calls,
            contingency_calls=contingency_calls,
            moderator=moderator,
            minimum_speaker_member_ids=(
                set(minimum_speaker_member_ids or [])
                if minimum_speaker_calls
                else set()
            ),
        )
        formal_path_call_ceiling_with_allowance = (
            projected_preflight_calls
            + maximum_speaker_calls
            + recommended_director_calls
            + optional_artifact_calls
        )
        maximum_total_calls = (
            projected_preflight_calls
            + maximum_discussion_calls
            + optional_artifact_calls
        )
        maximum_total_calls = max(maximum_total_calls, recommendation)

        blockers = self._route_blockers(routes)
        if not provider_status_available:
            blockers.insert(0, {"code": "PROVIDER_STATUS_UNAVAILABLE"})
        if minimum_speaker_member_ids is None:
            blockers.append({"code": "WORKFLOW_PROVIDER_COVERAGE_INSUFFICIENT"})
        if discussion_mode == "dynamic" and not moderator_route["callable"]:
            blockers.append({
                "code": "MODERATOR_PROVIDER_ROUTE_UNAVAILABLE",
                "member_id": moderator["id"],
            })
        if recommendation > DEFAULT_PROVIDER_CALL_HARD_LIMIT:
            blockers.append({
                "code": "RECOMMENDATION_EXCEEDS_DEPLOYMENT_HARD_LIMIT",
                "recommended_provider_calls": recommendation,
                "deployment_hard_limit": DEFAULT_PROVIDER_CALL_HARD_LIMIT,
            })

        capability_pack_ids = sorted({
            str(item or "").strip()
            for item in (
                room.get("active_capability_pack_ids")
                if isinstance(room.get("active_capability_pack_ids"), list)
                else room.get("capability_pack_ids") or []
            )
            if str(item or "").strip()
        })
        candidate_risk_review_required = (
            candidate_risk_review_protocol_required(
                workflow_policy,
                raw_members,
            )
        )
        protocols = {
            "turn_contract_version": TURN_CONTRACT_VERSION,
            "turn_contract_required": True,
            "turn_envelope_version": TURN_ENVELOPE_VERSION,
            "turn_envelope_schema_sha256": TURN_ENVELOPE_SCHEMA_SHA256,
            "provider_output_capabilities_version": OUTPUT_CAPABILITIES_VERSION,
            "candidate_risk_review_version": (
                CANDIDATE_RISK_REVIEW_VERSION
                if candidate_risk_review_required
                else None
            ),
            "candidate_risk_review_required": candidate_risk_review_required,
        }
        frozen_room = {
            "id": str(room.get("id") or clean_room_id),
            "settings_version": max(1, int(room.get("settings_version") or 1)),
            "discussion_mode": discussion_mode,
            "domain": str(room.get("domain") or "open_collaboration").strip(),
            "template_id": str(room.get("template_id") or "").strip(),
            "workflow_policy": copy.deepcopy(workflow_policy),
            "capability_pack_ids": capability_pack_ids,
            "plugin_registry_snapshot_sha256": str(
                room.get("plugin_registry_snapshot_sha256") or ""
            ),
            "protocols": protocols,
        }
        safety = {
            "budget_unit": "provider_call_count",
            "is_cost_estimate": False,
            "execution_capability": "none",
            "live_trading_allowed": False,
            "user_confirmation_required": True,
        }
        calls = {
            "version": PROVIDER_CALL_BUDGET_PROFILE_VERSION,
            "unit": "provider_call_count",
            "is_cost_estimate": False,
            "is_usage_forecast": False,
            "completion_assumes_valid_responses": True,
            "unique_preflight_route_count": len(routes),
            "projected_preflight_calls": projected_preflight_calls,
            "workflow_minimum_speaker_calls": workflow_minimum_speaker_calls,
            "minimum_speaker_calls": minimum_speaker_calls,
            "minimum_director_calls": minimum_director_calls,
            "recommended_director_calls": recommended_director_calls,
            "optional_artifact_calls": optional_artifact_calls,
            "contingency_calls": contingency_calls,
            "recommended_provider_calls": recommendation,
            "recommended_authorization_calls": recommendation,
            "projected_provider_calls_total": sum(
                item["projected_provider_calls"]
                for item in provider_call_projection
            ),
            "maximum_speaker_calls": maximum_speaker_calls,
            "maximum_director_calls": maximum_director_calls,
            "initial_dispatch_provider_calls": 0,
            "runtime_rules_first_can_reduce_calls": True,
            "core_success_path_calls": (
                projected_preflight_calls + minimum_speaker_calls
            ),
            "formal_path_call_ceiling_with_allowance": (
                formal_path_call_ceiling_with_allowance
            ),
            "formal_path_conservative_upper_bound": maximum_total_calls,
            "unprojected_call_kinds": ["round_interjection"],
            "absolute_ceiling_source": "user_authorized_max_provider_calls",
            "discussion_call_range": {
                "minimum": minimum_discussion_calls,
                "maximum": maximum_discussion_calls,
            },
            "total_call_range": {
                "minimum": recommendation,
                "maximum": maximum_total_calls,
            },
        }

        # settings_version and display data are audit/UI fields.  They are
        # intentionally outside the hash basis so a title-only update cannot
        # invalidate an otherwise identical behavioral plan.
        plan_version = (
            ROUND_LAUNCH_PLAN_VERSION_V5
            if canonical_round_context_authorizations is not None
            else ROUND_LAUNCH_PLAN_VERSION
        )
        hash_basis = {
            "version": plan_version,
            "objective": clean_objective,
            "room": {
                key: copy.deepcopy(value)
                for key, value in frozen_room.items()
                if key != "settings_version"
            },
            "members": copy.deepcopy(members),
            "moderator": copy.deepcopy(moderator),
            "moderator_selection_source": moderator_source,
            "skip_provider_ids": list(skip_ids),
            "preflight_routes": copy.deepcopy(routes),
            "provider_call_projection": copy.deepcopy(provider_call_projection),
            "calls": copy.deepcopy(calls),
            "safety": copy.deepcopy(safety),
        }
        if canonical_round_context_authorizations is not None:
            hash_basis["round_context_authorizations"] = copy.deepcopy(
                canonical_round_context_authorizations
            )
        plan_hash = _canonical_sha256(hash_basis)
        plan = {
            "version": plan_version,
            "plan_hash": plan_hash,
            "hash_algorithm": "sha256-canonical-json",
            "objective": clean_objective,
            "room": frozen_room,
            "display": {
                "room_title": str(room.get("title") or ""),
                "room_category": str(room.get("category") or ""),
            },
            "members": members,
            "moderator": {
                **moderator,
                "selection_source": moderator_source,
            },
            "skip_provider_ids": list(skip_ids),
            "preflight_routes": routes,
            "provider_call_projection": provider_call_projection,
            "calls": calls,
            "safety": safety,
            "ready_for_authorization": (
                workflow_ready
                and not blockers
            ),
            "blockers": blockers,
        }
        if canonical_round_context_authorizations is not None:
            plan["round_context_authorizations"] = copy.deepcopy(
                canonical_round_context_authorizations
            )
        if plan["ready_for_authorization"]:
            self._remember_recommendation(plan_hash, recommendation)
        return copy.deepcopy(plan)

    def validate_authorization(
        self,
        plan_hash: str,
        max_provider_calls: int,
    ) -> dict[str, Any]:
        clean_hash = str(plan_hash or "").strip().lower()
        recommendation = self._recommendations_by_hash.get(clean_hash)
        if recommendation is None:
            raise ValueError("plan_hash was not built by this launch-plan service")
        return validate_authorization(
            clean_hash,
            max_provider_calls,
            recommended_provider_calls=recommendation,
            expected_plan_hash=clean_hash,
        )

    def _remember_recommendation(self, plan_hash: str, recommendation: int) -> None:
        # Bound the in-memory validation index; it contains only hashes/counts.
        if plan_hash not in self._recommendations_by_hash and len(
            self._recommendations_by_hash
        ) >= 128:
            self._recommendations_by_hash.pop(next(iter(self._recommendations_by_hash)))
        self._recommendations_by_hash[plan_hash] = recommendation

    @staticmethod
    def _clean_skip_provider_ids(
        value: set[str] | list[str] | tuple[str, ...] | None,
    ) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, (set, list, tuple)):
            raise ValueError("skip_provider_ids must be a collection")
        if len(value) > 20:
            raise ValueError("skip_provider_ids may contain at most 20 items")
        clean: set[str] = set()
        for raw_provider_id in value:
            provider_id = str(raw_provider_id or "").strip().lower()
            if not provider_id or not _PROVIDER_ID_PATTERN.fullmatch(provider_id):
                raise ValueError("skip_provider_ids contains an invalid provider id")
            clean.add(provider_id)
        return tuple(sorted(clean))

    def _safe_provider_statuses(self) -> tuple[list[dict[str, Any]], bool]:
        try:
            raw_statuses = self.providers.status()
        except Exception:
            # Provider errors may contain credentials or upstream response data.
            return [], False
        if not isinstance(raw_statuses, list):
            return [], False
        safe: list[dict[str, Any]] = []
        for raw in raw_statuses:
            if not isinstance(raw, dict):
                continue
            provider_id = str(raw.get("id") or "").strip().lower()
            if not _PROVIDER_ID_PATTERN.fullmatch(provider_id):
                continue
            raw_output = (
                raw.get("output_capabilities")
                if isinstance(raw.get("output_capabilities"), dict)
                else {}
            )
            raw_modes = raw_output.get("modes")
            modes = tuple(
                mode
                for mode in OUTPUT_MODE_PRIORITY
                if isinstance(raw_modes, list) and mode in raw_modes
            )
            preferred_mode = str(
                raw_output.get("preferred_mode") or ""
            ).strip().lower()
            output_capabilities_valid = bool(
                raw_output.get("version") == OUTPUT_CAPABILITIES_VERSION
                and modes
                and preferred_mode in modes
            )
            if not output_capabilities_valid:
                modes = ("prompt_json",)
                preferred_mode = "prompt_json"
            safe.append({
                "id": provider_id,
                "model": str(raw.get("model") or "").strip()[:200],
                "configured": raw.get("configured") is True,
                "policy_disabled": raw.get("policy_disabled") is True,
                "output_capabilities": {
                    "version": OUTPUT_CAPABILITIES_VERSION,
                    "modes": list(modes),
                    "preferred_mode": preferred_mode,
                    "declared": bool(
                        output_capabilities_valid
                        and raw_output.get("declared") is True
                    ),
                },
            })
        return safe, True

    @staticmethod
    def _freeze_member(
        member: dict[str, Any],
        status_by_id: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        provider_id = str(member.get("provider") or "openai").strip().lower()
        configured_model = str(member.get("model") or "").strip()
        resolved_model = configured_model or str(
            (status_by_id.get(provider_id) or {}).get("model") or ""
        ).strip()
        return {
            "id": str(member.get("id") or ""),
            "version": max(1, int(member.get("version") or 1)),
            "name": str(member.get("name") or ""),
            "identity": str(member.get("identity") or ""),
            "stage": str(member.get("workflow_stage") or "flexible").strip().lower(),
            "provider": provider_id,
            "model": resolved_model,
        }

    @staticmethod
    def _freeze_moderator(
        room: dict[str, Any],
        members: list[dict[str, Any]],
        workflow_policy: dict[str, Any],
    ) -> tuple[dict[str, Any], str]:
        configured_id = str(room.get("moderator_member_id") or "").strip()
        if configured_id:
            configured = next(
                (member for member in members if member["id"] == configured_id),
                None,
            )
            if configured is None:
                raise ValueError("configured moderator is not an enabled room member")
            return copy.deepcopy(configured), "configured"
        first_stage = str((workflow_policy.get("stage_order") or [""])[0] or "")
        selected = next(
            (member for member in members if member["stage"] == first_stage),
            members[0],
        )
        return copy.deepcopy(selected), "workflow_stage_fallback"

    @staticmethod
    def _project_routes(
        members: list[dict[str, Any]],
        *,
        status_by_id: dict[str, dict[str, Any]],
        policy_disabled_ids: set[str],
        skip_ids: set[str],
    ) -> list[dict[str, Any]]:
        grouped: dict[tuple[str, str], list[str]] = {}
        for member in members:
            key = (member["provider"], member["model"])
            grouped.setdefault(key, []).append(member["id"])

        routes: list[dict[str, Any]] = []
        for provider_id, model in sorted(grouped):
            status = status_by_id.get(provider_id)
            known = status is not None
            configured = bool(status and status["configured"])
            policy_disabled = bool(
                provider_id in policy_disabled_ids
                or (status and status["policy_disabled"])
            )
            skipped = provider_id in skip_ids
            callable_route = known and configured and not policy_disabled and not skipped
            output_capabilities = (
                status.get("output_capabilities")
                if isinstance(status, dict)
                and isinstance(status.get("output_capabilities"), dict)
                else {
                    "version": OUTPUT_CAPABILITIES_VERSION,
                    "modes": ["prompt_json"],
                    "preferred_mode": "prompt_json",
                    "declared": False,
                }
            )
            routes.append({
                "provider": provider_id,
                "model": model,
                "member_ids": sorted(grouped[(provider_id, model)]),
                "known": known,
                "configured": configured,
                "policy_disabled": policy_disabled,
                "skipped": skipped,
                "callable": callable_route,
                "projected_preflight_calls": 1 if callable_route else 0,
                "output_capabilities_version": OUTPUT_CAPABILITIES_VERSION,
                "provider_output_modes": list(output_capabilities["modes"]),
                "turn_output_mode": str(output_capabilities["preferred_mode"]),
                "output_capabilities_declared": (
                    output_capabilities.get("declared") is True
                ),
            })
        return routes

    @staticmethod
    def _project_provider_calls(
        *,
        members: list[dict[str, Any]],
        routes: list[dict[str, Any]],
        statuses: list[dict[str, Any]],
        policy_disabled_ids: set[str],
        skip_ids: set[str],
        minimum_speaker_calls: int,
        minimum_director_calls: int,
        recommended_director_calls: int,
        optional_artifact_calls: int,
        contingency_calls: int,
        moderator: dict[str, Any],
        minimum_speaker_member_ids: set[str],
    ) -> list[dict[str, Any]]:
        callable_keys = {
            (route["provider"], route["model"])
            for route in routes
            if route["callable"]
        }
        callable_members = [
            member
            for member in members
            if (member["provider"], member["model"]) in callable_keys
        ]
        speaker_members = callable_members[:minimum_speaker_calls]
        if minimum_speaker_member_ids:
            speaker_members = [
                member
                for member in callable_members
                if member["id"] in minimum_speaker_member_ids
            ]
        speaker_by_route = Counter(
            (member["provider"], member["model"])
            for member in speaker_members
        )
        moderator_key = (moderator["provider"], moderator["model"])
        artifact_member = next(
            (member for member in callable_members if member["stage"] == "decision"),
            moderator if moderator_key in callable_keys else None,
        )
        artifact_key = (
            (artifact_member["provider"], artifact_member["model"])
            if artifact_member is not None
            else ("", "")
        )

        for route in routes:
            route_key = (route["provider"], route["model"])
            route["minimum_speaker_calls"] = int(speaker_by_route[route_key])
            route["minimum_director_calls"] = (
                minimum_director_calls if route_key == moderator_key else 0
            )
            route["recommended_director_calls"] = (
                recommended_director_calls if route_key == moderator_key else 0
            )
            route["optional_artifact_calls"] = (
                optional_artifact_calls if route_key == artifact_key else 0
            )
            route["contingency_calls"] = (
                contingency_calls if route_key == moderator_key else 0
            )
            route["projected_provider_calls"] = sum((
                int(route["projected_preflight_calls"]),
                route["minimum_speaker_calls"],
                route["minimum_director_calls"],
                route["recommended_director_calls"],
                route["optional_artifact_calls"],
                route["contingency_calls"],
            ))

        status_by_id = {item["id"]: item for item in statuses}
        provider_ids = sorted(
            set(status_by_id)
            | {route["provider"] for route in routes}
            | skip_ids
            | policy_disabled_ids
        )
        projection: list[dict[str, Any]] = []
        for provider_id in provider_ids:
            provider_routes = [
                route for route in routes if route["provider"] == provider_id
            ]
            status = status_by_id.get(provider_id)
            projection.append({
                "provider": provider_id,
                "known": status is not None,
                "configured": bool(status and status["configured"]),
                "policy_disabled": bool(
                    provider_id in policy_disabled_ids
                    or (status and status["policy_disabled"])
                ),
                "skipped": provider_id in skip_ids,
                "projected_preflight_calls": sum(
                    route["projected_preflight_calls"] for route in provider_routes
                ),
                "minimum_speaker_calls": sum(
                    route["minimum_speaker_calls"] for route in provider_routes
                ),
                "minimum_director_calls": sum(
                    route["minimum_director_calls"] for route in provider_routes
                ),
                "recommended_director_calls": sum(
                    route["recommended_director_calls"] for route in provider_routes
                ),
                "optional_artifact_calls": sum(
                    route["optional_artifact_calls"] for route in provider_routes
                ),
                "contingency_calls": sum(
                    route["contingency_calls"] for route in provider_routes
                ),
                "projected_provider_calls": sum(
                    route["projected_provider_calls"] for route in provider_routes
                ),
            })
        return projection

    @staticmethod
    def _minimum_workflow_speaker_member_ids(
        callable_members: list[dict[str, Any]],
        workflow_policy: dict[str, Any],
    ) -> list[str] | None:
        """Choose a deterministic set that satisfies all workflow minima.

        The greedy score favors a member that closes both a stage and a role
        gap.  Requirements are monotone (selecting a member never consumes a
        scarce slot), so any individually feasible set remains feasible as the
        set grows.
        """

        minimum_unique = int(workflow_policy["minimum_successful_members"])
        if len(callable_members) < minimum_unique:
            return None
        stage_minimums = {
            str(stage): int(minimum)
            for stage, minimum in workflow_policy["minimum_stage_coverage"].items()
        }
        requirements = list(workflow_policy["required_coverage"])

        def matches(member: dict[str, Any], requirement: dict[str, Any]) -> bool:
            selectors = requirement["any_of"]
            stances = set(selectors.get("stances") or [])
            capabilities = set(selectors.get("capabilities") or [])
            return (
                str(member.get("stance") or "").strip().lower() in stances
                or bool({
                    str(item or "").strip().lower()
                    for item in (member.get("capabilities") or [])
                    if str(item or "").strip()
                } & capabilities)
            )

        # Reject impossible constraints before selection.
        stage_counts = Counter(
            str(member.get("workflow_stage") or "flexible").strip().lower()
            for member in callable_members
        )
        if any(stage_counts[stage] < minimum for stage, minimum in stage_minimums.items()):
            return None
        if any(
            sum(1 for member in callable_members if matches(member, requirement))
            < int(requirement["minimum"])
            for requirement in requirements
        ):
            return None

        selected_indexes: set[int] = set()
        while True:
            selected = [
                member
                for index, member in enumerate(callable_members)
                if index in selected_indexes
            ]
            selected_stage_counts = Counter(
                str(member.get("workflow_stage") or "flexible").strip().lower()
                for member in selected
            )
            stage_deficits = {
                stage: max(0, minimum - selected_stage_counts[stage])
                for stage, minimum in stage_minimums.items()
            }
            requirement_deficits = [
                max(
                    0,
                    int(requirement["minimum"])
                    - sum(1 for member in selected if matches(member, requirement)),
                )
                for requirement in requirements
            ]
            unique_deficit = max(0, minimum_unique - len(selected_indexes))
            if (
                not any(stage_deficits.values())
                and not any(requirement_deficits)
                and unique_deficit == 0
            ):
                break

            best_index = -1
            best_score = 0
            for index, member in enumerate(callable_members):
                if index in selected_indexes:
                    continue
                stage = str(
                    member.get("workflow_stage") or "flexible"
                ).strip().lower()
                score = int(stage_deficits.get(stage, 0) > 0)
                score += sum(
                    1
                    for requirement, deficit in zip(
                        requirements,
                        requirement_deficits,
                        strict=True,
                    )
                    if deficit > 0 and matches(member, requirement)
                )
                score += int(unique_deficit > 0)
                if score > best_score:
                    best_index = index
                    best_score = score
            if best_index < 0:
                return None
            selected_indexes.add(best_index)

        return [
            str(member.get("id") or "")
            for index, member in enumerate(callable_members)
            if index in selected_indexes
        ]

    @staticmethod
    def _route_blockers(routes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        blockers: list[dict[str, Any]] = []
        for route in routes:
            safe_route = {
                "provider": route["provider"],
                "model": route["model"],
            }
            if route["skipped"]:
                blockers.append({"code": "PROVIDER_SKIPPED", **safe_route})
            elif route["policy_disabled"]:
                blockers.append({"code": "PROVIDER_POLICY_DISABLED", **safe_route})
            elif not route["known"]:
                blockers.append({"code": "PROVIDER_UNKNOWN", **safe_route})
            elif not route["configured"]:
                blockers.append({"code": "PROVIDER_NOT_CONFIGURED", **safe_route})
        return blockers


__all__ = [
    "DEFAULT_PROVIDER_CALL_HARD_LIMIT",
    "ROUND_LAUNCH_AUTHORIZATION_VERSION",
    "ROUND_LAUNCH_PLAN_VERSION",
    "ROUND_LAUNCH_PLAN_VERSION_V4",
    "ROUND_LAUNCH_PLAN_VERSION_V5",
    "RoundLaunchPlanService",
    "validate_authorization",
]
