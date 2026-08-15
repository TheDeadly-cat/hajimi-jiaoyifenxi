from __future__ import annotations

import re
import secrets
import time
from typing import TYPE_CHECKING, Any

from .providers.output import OUTPUT_CAPABILITIES_VERSION, OUTPUT_MODE_PRIORITY
from .providers.registry import ProviderRegistry
from .project_round_focus import normalize_project_round_focus_authorization
from .round_launch_plan import (
    ROUND_LAUNCH_PLAN_VERSION,
    ROUND_LAUNCH_PLAN_VERSION_V4,
    ROUND_LAUNCH_PLAN_VERSION_V5,
    _canonical_sha256,
)
from .round_contexts import (
    build_round_context_authorization_set,
    normalize_round_context_authorizations,
    round_context_authorization_entry,
)
from .store import StudioStore
from .turn_envelope import (
    TURN_ENVELOPE_SCHEMA_SHA256,
    TURN_ENVELOPE_VERSION,
)
from .turn_contract import (
    CANDIDATE_RISK_REVIEW_VERSION,
    TURN_CONTRACT_VERSION,
)

if TYPE_CHECKING:
    from .provider_call_ledger import ProviderCallLedger


_PLAN_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_PROVIDER_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
_MEMBER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_SAFE_PREFLIGHT_ERROR_CODES = frozenset({
    "PROVIDER_CALL_BUDGET_EXCEEDED",
    "PROVIDER_CALL_LEDGER_FAILED",
    "PROVIDER_POLICY_DISABLED",
    "PROVIDER_SKIPPED",
    "authentication_or_model_access_denied",
    "connection_failed",
    "empty_response",
    "invalid_response",
    "model_not_configured",
    "model_not_found",
    "not_configured",
    "probe_failed",
    "provider_not_supported",
    "provider_preflight_failed",
    "provider_unavailable",
    "quota_exhausted",
    "rate_limited",
    "request_rejected",
    "timeout",
})


class ProviderPreflightService:
    def __init__(self, store: StudioStore, providers: ProviderRegistry) -> None:
        self.store = store
        self.providers = providers

    def check_room(
        self,
        room_id: str,
        *,
        member_ids: list[str] | None = None,
        skip_provider_ids: set[str] | None = None,
        ledger: ProviderCallLedger | None = None,
    ) -> dict[str, Any]:
        snapshot = self.store.room_snapshot(room_id)
        if not snapshot:
            raise LookupError("房间不存在")

        all_members = list(snapshot.get("members") or [])
        by_id = {str(member.get("id") or ""): member for member in all_members}
        requested_issues: list[dict[str, Any]] = []
        if member_ids is None:
            members = [member for member in all_members if member.get("enabled")]
        else:
            members = []
            seen_member_ids: set[str] = set()
            for member_id in member_ids:
                clean_id = str(member_id or "").strip()
                if not clean_id or clean_id in seen_member_ids:
                    continue
                seen_member_ids.add(clean_id)
                member = by_id.get(clean_id)
                if not member:
                    requested_issues.append({
                        "id": clean_id,
                        "name": "",
                        "available": False,
                        "is_moderator": False,
                        "provider": "",
                        "model": "",
                        "error_code": "member_not_found",
                        "message": "指定成员不存在。",
                    })
                elif not member.get("enabled"):
                    requested_issues.append({
                        "id": clean_id,
                        "name": str(member.get("name") or ""),
                        "available": False,
                        "is_moderator": False,
                        "provider": str(member.get("provider") or "").lower(),
                        "model": str(member.get("model") or ""),
                        "error_code": "member_disabled",
                        "message": "该成员当前已暂停。",
                    })
                else:
                    members.append(member)

        provider_checks = self.providers.preflight(
            members,
            skip_provider_ids=skip_provider_ids,
            ledger=ledger,
        )
        checks_by_key = {
            (str(item.get("provider") or ""), str(item.get("model") or "")): item
            for item in provider_checks
        }

        room = snapshot.get("room") or {}
        workflow_policy = room.get("workflow_policy") or {}
        stage_order = workflow_policy.get("stage_order") or []
        first_stage = str(stage_order[0] or "") if stage_order else ""
        configured_moderator_id = str(
            room.get("moderator_member_id") or ""
        ).strip()
        moderator = next(
            (
                member for member in members
                if configured_moderator_id
                and str(member.get("id") or "") == configured_moderator_id
            ),
            None,
        )
        if not moderator and not configured_moderator_id:
            moderator = next(
                (
                    member for member in members
                    if first_stage and str(member.get("workflow_stage") or "") == first_stage
                ),
                None,
            )
        if not moderator:
            moderator = (
                None
                if configured_moderator_id
                else next(
                    (member for member in members if str(member.get("stance") or "") == "facilitator"),
                    members[0] if members else None,
                )
            )
        moderator_id = str((moderator or {}).get("id") or "")

        grouped_members: dict[tuple[str, str], list[dict[str, Any]]] = {}
        member_states: list[dict[str, Any]] = []
        for member in members:
            provider_id = str(member.get("provider") or "").strip().lower()
            selected_model = self.providers.resolved_model(
                provider_id,
                str(member.get("model") or ""),
            )
            key = (provider_id, selected_model)
            grouped_members.setdefault(key, []).append(member)
            check = checks_by_key.get(key) or {}
            member_states.append({
                "id": str(member.get("id") or ""),
                "name": str(member.get("name") or ""),
                "available": bool(check.get("ready")),
                "is_moderator": str(member.get("id") or "") == moderator_id,
                "provider": provider_id,
                "model": selected_model,
                "error_code": str(check.get("error_code") or ""),
                "message": str(check.get("message") or ""),
            })

        enriched_checks: list[dict[str, Any]] = []
        for check in provider_checks:
            key = (
                str(check.get("provider") or ""),
                str(check.get("model") or ""),
            )
            assigned = grouped_members.get(key, [])
            enriched_checks.append({
                **check,
                "member_count": len(assigned),
                "member_ids": [str(member.get("id") or "") for member in assigned],
                "member_names": [str(member.get("name") or "") for member in assigned],
            })

        unavailable_members = [
            *requested_issues,
            *(state for state in member_states if not state["available"]),
        ]
        moderator_state = next(
            (state for state in member_states if state["is_moderator"]),
            {
                "id": configured_moderator_id,
                "name": str((by_id.get(configured_moderator_id) or {}).get("name") or ""),
                "available": False,
                "is_moderator": True,
                "provider": str((by_id.get(configured_moderator_id) or {}).get("provider") or ""),
                "model": str((by_id.get(configured_moderator_id) or {}).get("model") or ""),
                "error_code": (
                    "moderator_not_selected"
                    if configured_moderator_id and configured_moderator_id in by_id
                    else "moderator_missing"
                ),
                "message": (
                    "房间指定的主持成员未参加本轮或已暂停。"
                    if configured_moderator_id
                    else "当前没有可参加本轮的主持成员。"
                ),
            },
        )
        ready = (
            bool(member_states)
            and not requested_issues
            and bool(moderator_state.get("available"))
            and not any(not state["available"] for state in member_states)
        )
        return {
            "room_id": room_id,
            "checked_at": int(time.time() * 1000),
            "ready": ready,
            "member_count": len(member_states),
            "provider_check_count": len(enriched_checks),
            "provider_checks": enriched_checks,
            "members": member_states,
            "moderator": moderator_state,
            "unavailable_members": unavailable_members,
            "blocking": {
                "moderator_unavailable": not bool(moderator_state.get("available")),
                "unavailable_member_count": len(unavailable_members),
            },
        }

    def check_launch_plan(
        self,
        room_id: str,
        *,
        launch_plan: dict[str, Any],
        skip_provider_ids: set[str] | None,
        ledger: ProviderCallLedger,
    ) -> dict[str, Any]:
        """Probe only the routes frozen in one confirmed new-round plan.

        Unlike :meth:`check_room`, this path deliberately does not read current
        room members.  The confirmed plan and its exact unbound round ledger are
        the complete authorization boundary for every provider/model probe.
        """

        clean_room_id = str(room_id or "").strip()
        if not clean_room_id:
            raise ValueError("launch-plan preflight requires a room id")
        plan_hash, members, moderator, plan_skip_ids, routes = (
            self._validate_launch_plan(clean_room_id, launch_plan)
        )
        requested_skip_ids = self._clean_launch_skip_ids(
            skip_provider_ids,
            field="launch-plan preflight skip policy",
        )
        if requested_skip_ids != plan_skip_ids:
            raise ValueError("launch-plan preflight skip policy does not match the plan")
        if ledger is None:
            raise ValueError("launch-plan preflight requires a Provider-call ledger")
        try:
            execution = ledger.snapshot()
        except Exception:
            raise ValueError(
                "launch-plan preflight Provider-call authorization is invalid"
            ) from None
        if (
            str(execution.get("room_id") or "") != clean_room_id
            or str(execution.get("scope") or "") != "round"
            or str(execution.get("round_id") or "")
        ):
            raise ValueError(
                "launch-plan preflight requires the exact unbound round ledger"
            )
        if not secrets.compare_digest(
            str(execution.get("plan_hash") or "").strip().lower(),
            plan_hash,
        ):
            raise ValueError("launch-plan preflight ledger does not match the plan")
        persisted_skip_policy = execution.get("skip_policy")
        if not isinstance(persisted_skip_policy, dict):
            raise ValueError("launch-plan preflight ledger skip policy is invalid")
        ledger_skip_ids = self._clean_launch_skip_ids(
            persisted_skip_policy.get("provider_ids"),
            field="launch-plan preflight ledger skip policy",
        )
        if ledger_skip_ids != plan_skip_ids:
            raise ValueError("launch-plan preflight ledger skip policy does not match the plan")
        max_calls = execution.get("max_calls")
        if (
            isinstance(max_calls, bool)
            or not isinstance(max_calls, int)
            or max_calls < 1
            or max_calls > 100
        ):
            raise ValueError("launch-plan preflight ledger limit is invalid")
        output_mode_by_route = {
            (
                str(route.get("provider") or ""),
                str(route.get("model") or ""),
            ): str(route.get("turn_output_mode") or "")
            for route in launch_plan["preflight_routes"]
            if isinstance(route, dict)
        }
        expected_member_routes = {
            "version": "provider_member_routes_v2",
            "members": sorted(
                [
                    {
                        "member_id": str(member["id"]),
                        "approved_member_version": int(member["version"]),
                        "provider": str(member["provider"]),
                        "model": str(member["model"]),
                        "turn_output_mode": output_mode_by_route[
                            (str(member["provider"]), str(member["model"]))
                        ],
                        "turn_envelope_version": TURN_ENVELOPE_VERSION,
                        "turn_envelope_schema_sha256": (
                            TURN_ENVELOPE_SCHEMA_SHA256
                        ),
                    }
                    for member in members
                ],
                key=lambda item: item["member_id"],
            ),
        }
        if (
            execution.get("member_routes_present") is not True
            or execution.get("member_routes_integrity_ok") is not True
            or execution.get("member_routes") != expected_member_routes
        ):
            raise ValueError(
                "launch-plan preflight ledger member routes do not match the plan"
            )

        assignments = [dict(member) for member in members]
        try:
            raw_checks = self.providers.preflight(
                assignments,
                skip_provider_ids=set(plan_skip_ids),
                cache_ttl_seconds=0,
                ledger=ledger,
            )
        except Exception:
            raise RuntimeError(
                "launch-plan Provider preflight could not be completed safely"
            ) from None
        checks = raw_checks if isinstance(raw_checks, list) else []
        checks_by_key: dict[tuple[str, str], dict[str, Any]] = {}
        for raw_check in checks:
            if not isinstance(raw_check, dict):
                continue
            key = (
                str(raw_check.get("provider") or "").strip().lower(),
                str(raw_check.get("model") or "").strip(),
            )
            if key in routes and key not in checks_by_key:
                checks_by_key[key] = raw_check

        grouped_members: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for member in members:
            grouped_members.setdefault(
                (member["provider"], member["model"]),
                [],
            ).append(member)

        provider_checks: list[dict[str, Any]] = []
        safe_checks_by_key: dict[tuple[str, str], dict[str, Any]] = {}
        for provider_id, model in routes:
            safe_check = self._safe_launch_provider_check(
                provider_id,
                model,
                checks_by_key.get((provider_id, model)),
            )
            assigned = grouped_members[(provider_id, model)]
            safe_check.update({
                "member_count": len(assigned),
                "member_ids": [member["id"] for member in assigned],
                "member_names": [member["name"] for member in assigned],
            })
            provider_checks.append(safe_check)
            safe_checks_by_key[(provider_id, model)] = safe_check

        moderator_id = moderator["id"]
        member_states: list[dict[str, Any]] = []
        for member in members:
            check = safe_checks_by_key[(member["provider"], member["model"])]
            available = check["ready"] is True
            member_states.append({
                "id": member["id"],
                "name": member["name"],
                "version": member["version"],
                "available": available,
                "is_moderator": member["id"] == moderator_id,
                "provider": member["provider"],
                "model": member["model"],
                "route_source": "launch_plan",
                "error_code": "" if available else check["error_code"],
                "message": check["message"],
            })
        moderator_state = next(
            state for state in member_states if state["id"] == moderator_id
        )
        unavailable_members = [
            state for state in member_states if not state["available"]
        ]
        ready = bool(member_states) and not unavailable_members and bool(
            moderator_state["available"]
        )
        return {
            "room_id": clean_room_id,
            "plan_hash": plan_hash,
            "route_source": "launch_plan",
            "checked_at": int(time.time() * 1000),
            "ready": ready,
            "member_count": len(member_states),
            "provider_check_count": len(provider_checks),
            "provider_checks": provider_checks,
            "members": member_states,
            "moderator": moderator_state,
            "unavailable_members": unavailable_members,
            "blocking": {
                "moderator_unavailable": not bool(moderator_state["available"]),
                "unavailable_member_count": len(unavailable_members),
            },
        }

    @staticmethod
    def _clean_launch_skip_ids(value: Any, *, field: str) -> set[str]:
        if value is None:
            return set()
        if not isinstance(value, (set, frozenset, list, tuple)):
            raise ValueError(f"{field} is invalid")
        clean: set[str] = set()
        for raw_provider_id in value:
            if not isinstance(raw_provider_id, str):
                raise ValueError(f"{field} is invalid")
            provider_id = raw_provider_id.strip().lower()
            if not _PROVIDER_ID_PATTERN.fullmatch(provider_id):
                raise ValueError(f"{field} is invalid")
            clean.add(provider_id)
        return clean

    @classmethod
    def _validate_launch_plan(
        cls,
        room_id: str,
        launch_plan: Any,
    ) -> tuple[
        str,
        list[dict[str, Any]],
        dict[str, Any],
        set[str],
        list[tuple[str, str]],
    ]:
        if not isinstance(launch_plan, dict):
            raise ValueError("launch plan is invalid")
        plan_version = launch_plan.get("version")
        expected_plan_keys = {
            "version",
            "plan_hash",
            "hash_algorithm",
            "objective",
            "room",
            "display",
            "members",
            "moderator",
            "skip_provider_ids",
            "preflight_routes",
            "provider_call_projection",
            "calls",
            "safety",
            "ready_for_authorization",
            "blockers",
        }
        focus_authorization: dict[str, Any] | None = None
        round_context_authorizations: dict[str, Any] | None = None
        if plan_version == ROUND_LAUNCH_PLAN_VERSION_V4:
            expected_plan_keys.add("project_round_focus_authorization")
            focus_authorization = normalize_project_round_focus_authorization(
                launch_plan.get("project_round_focus_authorization")
            )
            if focus_authorization != launch_plan.get(
                "project_round_focus_authorization"
            ):
                raise ValueError("launch plan round-focus authorization is not canonical")
        elif plan_version == ROUND_LAUNCH_PLAN_VERSION_V5:
            expected_plan_keys.add("round_context_authorizations")
            raw_authorizations = launch_plan.get("round_context_authorizations")
            normalized_authorizations = normalize_round_context_authorizations(
                raw_authorizations
            )
            round_context_authorizations = (
                build_round_context_authorization_set(
                    round_context_authorization_entry(*key, request)
                    for key, request in normalized_authorizations.items()
                )
            )
            if (
                not normalized_authorizations
                or round_context_authorizations != raw_authorizations
            ):
                raise ValueError(
                    "launch plan round-context authorizations are not canonical"
                )
        elif plan_version != ROUND_LAUNCH_PLAN_VERSION:
            raise ValueError("launch plan version is unsupported")
        if set(launch_plan) != expected_plan_keys:
            raise ValueError("launch plan has an invalid closed shape")
        required_types = {
            "room": dict,
            "members": list,
            "moderator": dict,
            "skip_provider_ids": list,
            "preflight_routes": list,
            "provider_call_projection": list,
            "calls": dict,
            "safety": dict,
            "blockers": list,
        }
        if any(
            not isinstance(launch_plan.get(key), expected_type)
            for key, expected_type in required_types.items()
        ):
            raise ValueError("launch plan structure is invalid")
        if (
            launch_plan.get("hash_algorithm") != "sha256-canonical-json"
            or launch_plan.get("ready_for_authorization") is not True
            or launch_plan.get("blockers") != []
        ):
            raise ValueError("launch plan is not authorized for Provider preflight")
        plan_hash = str(launch_plan.get("plan_hash") or "").strip().lower()
        if not _PLAN_HASH_PATTERN.fullmatch(plan_hash):
            raise ValueError("launch plan hash is invalid")
        objective = launch_plan.get("objective")
        if not isinstance(objective, str) or not objective.strip():
            raise ValueError("launch plan objective is invalid")

        room = launch_plan["room"]
        if not isinstance(room.get("id"), str) or room["id"] != room_id:
            raise ValueError("launch plan does not belong to this room")
        expected_room_keys = {
            "id",
            "settings_version",
            "discussion_mode",
            "domain",
            "template_id",
            "workflow_policy",
            "capability_pack_ids",
            "plugin_registry_snapshot_sha256",
            "protocols",
        }
        if set(room) != expected_room_keys:
            raise ValueError("launch plan room structure is invalid")
        plugin_registry_snapshot_sha256 = room.get(
            "plugin_registry_snapshot_sha256"
        )
        if (
            not isinstance(plugin_registry_snapshot_sha256, str)
            or (
                plugin_registry_snapshot_sha256
                and not _PLAN_HASH_PATTERN.fullmatch(
                    plugin_registry_snapshot_sha256
                )
            )
        ):
            raise ValueError("launch plan plugin registry seal is invalid")
        protocols = room.get("protocols")
        if not isinstance(protocols, dict) or set(protocols) != {
            "turn_contract_version",
            "turn_contract_required",
            "turn_envelope_version",
            "turn_envelope_schema_sha256",
            "provider_output_capabilities_version",
            "candidate_risk_review_version",
            "candidate_risk_review_required",
        }:
            raise ValueError("launch plan protocol structure is invalid")
        risk_review_required = protocols.get("candidate_risk_review_required")
        expected_risk_review_version = (
            CANDIDATE_RISK_REVIEW_VERSION
            if risk_review_required is True
            else None
        )
        if (
            protocols.get("turn_contract_version") != TURN_CONTRACT_VERSION
            or protocols.get("turn_contract_required") is not True
            or protocols.get("turn_envelope_version") != TURN_ENVELOPE_VERSION
            or protocols.get("turn_envelope_schema_sha256")
            != TURN_ENVELOPE_SCHEMA_SHA256
            or protocols.get("provider_output_capabilities_version")
            != OUTPUT_CAPABILITIES_VERSION
            or not isinstance(risk_review_required, bool)
            or protocols.get("candidate_risk_review_version")
            != expected_risk_review_version
        ):
            raise ValueError("launch plan protocol markers are invalid")

        raw_plan_skip_ids = launch_plan["skip_provider_ids"]
        plan_skip_ids = cls._clean_launch_skip_ids(
            raw_plan_skip_ids,
            field="launch plan skip policy",
        )
        if raw_plan_skip_ids != sorted(plan_skip_ids):
            raise ValueError("launch plan skip policy is not canonical")

        expected_member_keys = {
            "id",
            "version",
            "name",
            "identity",
            "stage",
            "provider",
            "model",
        }
        members: list[dict[str, Any]] = []
        member_ids: set[str] = set()
        for raw_member in launch_plan["members"]:
            if not isinstance(raw_member, dict) or set(raw_member) != expected_member_keys:
                raise ValueError("launch plan member structure is invalid")
            raw_member_id = raw_member.get("id")
            raw_provider_id = raw_member.get("provider")
            raw_model = raw_member.get("model")
            if not all(
                isinstance(value, str)
                for value in (raw_member_id, raw_provider_id, raw_model)
            ):
                raise ValueError("launch plan member route is invalid")
            member_id = raw_member_id
            version = raw_member.get("version")
            provider_id = raw_provider_id
            model = raw_model
            if (
                not _MEMBER_ID_PATTERN.fullmatch(member_id)
                or member_id in member_ids
                or isinstance(version, bool)
                or not isinstance(version, int)
                or version < 1
                or not _PROVIDER_ID_PATTERN.fullmatch(provider_id)
                or provider_id in plan_skip_ids
                or not model
                or model != model.strip()
                or len(model) > 200
            ):
                raise ValueError("launch plan member route is invalid")
            if any(
                not isinstance(raw_member.get(key), str)
                for key in ("name", "identity", "stage")
            ):
                raise ValueError("launch plan member text is invalid")
            member_ids.add(member_id)
            members.append(dict(raw_member))
        if not members or len(members) > 200:
            raise ValueError("launch plan members are invalid")

        raw_moderator = launch_plan["moderator"]
        expected_moderator_keys = expected_member_keys | {"selection_source"}
        if set(raw_moderator) != expected_moderator_keys:
            raise ValueError("launch plan moderator structure is invalid")
        moderator_id = str(raw_moderator.get("id") or "")
        moderator_member = next(
            (member for member in members if member["id"] == moderator_id),
            None,
        )
        moderator = {
            key: raw_moderator[key] for key in expected_member_keys
        }
        if (
            moderator_member != moderator
            or raw_moderator.get("selection_source")
            not in {"configured", "workflow_stage_fallback"}
        ):
            raise ValueError("launch plan moderator does not match a frozen member")

        expected_routes: dict[tuple[str, str], list[str]] = {}
        for member in members:
            expected_routes.setdefault(
                (member["provider"], member["model"]),
                [],
            ).append(member["id"])
        routes: list[tuple[str, str]] = []
        seen_routes: set[tuple[str, str]] = set()
        for raw_route in launch_plan["preflight_routes"]:
            if not isinstance(raw_route, dict):
                raise ValueError("launch plan preflight route is invalid")
            provider_id = raw_route.get("provider")
            model = raw_route.get("model")
            if not isinstance(provider_id, str) or not isinstance(model, str):
                raise ValueError("launch plan preflight route is invalid")
            key = (provider_id, model)
            raw_member_ids = raw_route.get("member_ids")
            raw_output_modes = raw_route.get("provider_output_modes")
            output_modes = (
                [
                    mode
                    for mode in OUTPUT_MODE_PRIORITY
                    if isinstance(raw_output_modes, list)
                    and mode in raw_output_modes
                ]
                if isinstance(raw_output_modes, list)
                else []
            )
            turn_output_mode = raw_route.get("turn_output_mode")
            if (
                key not in expected_routes
                or key in seen_routes
                or not isinstance(raw_member_ids, list)
                or any(not isinstance(member_id, str) for member_id in raw_member_ids)
                or raw_member_ids != sorted(expected_routes[key])
                or raw_route.get("known") is not True
                or raw_route.get("configured") is not True
                or raw_route.get("policy_disabled") is not False
                or raw_route.get("skipped") is not False
                or raw_route.get("callable") is not True
                or raw_route.get("projected_preflight_calls") != 1
                or raw_route.get("output_capabilities_version")
                != OUTPUT_CAPABILITIES_VERSION
                or raw_output_modes != output_modes
                or not output_modes
                or turn_output_mode != output_modes[0]
                or not isinstance(
                    raw_route.get("output_capabilities_declared"),
                    bool,
                )
            ):
                raise ValueError("launch plan preflight route is invalid")
            seen_routes.add(key)
            routes.append(key)
        if seen_routes != set(expected_routes):
            raise ValueError("launch plan preflight routes are incomplete")

        calls = launch_plan["calls"]
        safety = launch_plan["safety"]
        if (
            calls.get("unit") != "provider_call_count"
            or calls.get("is_cost_estimate") is not False
            or calls.get("unique_preflight_route_count") != len(routes)
            or calls.get("projected_preflight_calls") != len(routes)
            or safety.get("budget_unit") != "provider_call_count"
            or safety.get("is_cost_estimate") is not False
            or safety.get("execution_capability") != "none"
            or safety.get("live_trading_allowed") is not False
            or safety.get("user_confirmation_required") is not True
        ):
            raise ValueError("launch plan safety or call projection is invalid")

        moderator_hash_value = {
            key: raw_moderator[key] for key in expected_member_keys
        }
        hash_basis = {
            "version": launch_plan["version"],
            "objective": objective,
            "room": {
                key: value for key, value in room.items()
                if key != "settings_version"
            },
            "members": launch_plan["members"],
            "moderator": moderator_hash_value,
            "moderator_selection_source": raw_moderator["selection_source"],
            "skip_provider_ids": raw_plan_skip_ids,
            "preflight_routes": launch_plan["preflight_routes"],
            "provider_call_projection": launch_plan["provider_call_projection"],
            "calls": calls,
            "safety": safety,
        }
        if plan_version == ROUND_LAUNCH_PLAN_VERSION_V4:
            hash_basis["project_round_focus_authorization"] = focus_authorization
        elif plan_version == ROUND_LAUNCH_PLAN_VERSION_V5:
            hash_basis["round_context_authorizations"] = (
                round_context_authorizations
            )
        try:
            calculated_hash = _canonical_sha256(hash_basis)
        except (TypeError, ValueError):
            raise ValueError("launch plan cannot be verified") from None
        if not secrets.compare_digest(calculated_hash, plan_hash):
            raise ValueError("launch plan hash does not match its frozen routes")
        return plan_hash, members, moderator, plan_skip_ids, routes

    @staticmethod
    def _safe_launch_provider_check(
        provider_id: str,
        model: str,
        raw_check: dict[str, Any] | None,
    ) -> dict[str, Any]:
        check = raw_check if isinstance(raw_check, dict) else {}
        configured = check.get("configured") is True
        reachable = check.get("reachable") is True
        model_access = check.get("model_access") is True
        ready = (
            check.get("ready") is True
            and configured
            and reachable
            and model_access
        )
        raw_error_code = str(check.get("error_code") or "").strip()
        error_code = (
            raw_error_code
            if raw_error_code in _SAFE_PREFLIGHT_ERROR_CODES
            else "provider_preflight_failed"
        )
        if ready:
            error_code = ""
        try:
            latency_ms = max(0, min(int(check.get("latency_ms") or 0), 604_800_000))
        except (TypeError, ValueError):
            latency_ms = 0
        return {
            "provider": provider_id,
            "model": model,
            "configured": configured,
            "reachable": reachable,
            "model_access": model_access,
            "latency_ms": latency_ms,
            "cached": check.get("cached") is True,
            "ready": ready,
            "error_code": error_code,
            "message": (
                "Provider route is ready."
                if ready
                else "Provider route is unavailable."
            ),
        }

    def _check_approved_resume_members(
        self,
        room_id: str,
        *,
        current_by_id: dict[str, dict[str, Any]],
        member_ids: list[str],
        approved_member_routes: dict[str, dict[str, Any]],
        skip_provider_ids: set[str] | None,
        ledger: ProviderCallLedger,
    ) -> dict[str, Any]:
        """Probe current identities through the formal round's approved routes."""

        assignments: list[dict[str, Any]] = []
        unavailable: list[dict[str, Any]] = []
        seen_member_ids: set[str] = set()
        for raw_member_id in member_ids:
            member_id = str(raw_member_id or "").strip()
            if not member_id or member_id in seen_member_ids:
                continue
            seen_member_ids.add(member_id)
            member = current_by_id.get(member_id)
            route = approved_member_routes.get(member_id)
            route_provider = str((route or {}).get("provider") or "").strip().lower()
            route_model = str((route or {}).get("model") or "").strip()
            try:
                current_version = int((member or {}).get("version") or 0)
                approved_version = int(
                    (route or {}).get("approved_member_version") or 0
                )
            except (TypeError, ValueError):
                current_version = 0
                approved_version = 0
            error_code = ""
            message = ""
            if not member:
                error_code = "member_not_found"
                message = "指定成员不存在。"
            elif not member.get("enabled"):
                error_code = "member_disabled"
                message = "该成员当前已暂停。"
            elif (
                not route
                or approved_version < 1
                or current_version < approved_version
                or not route_provider
                or not route_model
            ):
                error_code = "member_route_not_authorized"
                message = "该成员不在本轮已确认的 Provider 路由中。"
            if error_code:
                unavailable.append({
                    "id": member_id,
                    "name": str((member or {}).get("name") or ""),
                    "available": False,
                    "is_moderator": False,
                    "provider": route_provider,
                    "model": route_model,
                    "route_source": "approved_round_ledger",
                    "error_code": error_code,
                    "message": message,
                })
                continue
            assignments.append({
                **member,
                "provider": route_provider,
                "model": route_model,
            })

        provider_checks = self.providers.preflight(
            assignments,
            skip_provider_ids=skip_provider_ids,
            ledger=ledger,
        )
        checks_by_key = {
            (str(check.get("provider") or ""), str(check.get("model") or "")): check
            for check in provider_checks
            if isinstance(check, dict)
        }
        grouped_members: dict[tuple[str, str], list[dict[str, Any]]] = {}
        member_states: list[dict[str, Any]] = []
        for member in assignments:
            key = (
                str(member.get("provider") or "").strip().lower(),
                str(member.get("model") or "").strip(),
            )
            grouped_members.setdefault(key, []).append(member)
            check = checks_by_key.get(key) or {}
            member_states.append({
                "id": str(member.get("id") or ""),
                "name": str(member.get("name") or ""),
                "available": bool(check.get("ready")),
                "is_moderator": False,
                "provider": key[0],
                "model": key[1],
                "version": int(member.get("version") or 1),
                "route_source": "approved_round_ledger",
                "error_code": str(check.get("error_code") or ""),
                "message": str(check.get("message") or ""),
            })
        enriched_checks: list[dict[str, Any]] = []
        for check in provider_checks:
            key = (
                str(check.get("provider") or ""),
                str(check.get("model") or ""),
            )
            assigned = grouped_members.get(key, [])
            enriched_checks.append({
                **check,
                "member_count": len(assigned),
                "member_ids": [str(member.get("id") or "") for member in assigned],
                "member_names": [str(member.get("name") or "") for member in assigned],
            })
        unavailable.extend(
            state for state in member_states if not state["available"]
        )
        return {
            "room_id": room_id,
            "checked_at": int(time.time() * 1000),
            "ready": bool(member_states) and not unavailable,
            "member_count": len(member_states),
            "provider_checks": enriched_checks,
            "members": member_states,
            "unavailable_members": unavailable,
        }

    def check_resume_round(
        self,
        room_id: str,
        *,
        checkpoint_state: dict[str, Any],
        member_ids: list[str],
        skip_provider_ids: set[str] | None = None,
        ledger: ProviderCallLedger | None = None,
    ) -> dict[str, Any]:
        """Probe the routes a paused round will actually use when resumed.

        Ordinary members use their latest identity fields on their next turn,
        while a current formal ledger keeps the provider/model route approved at
        launch.  The hidden moderator remains frozen by the v7 checkpoint and
        must also agree with that ledger.  Legacy ledgers without a route manifest
        retain the prior current-member compatibility behavior.
        """

        snapshot = self.store.room_snapshot(room_id)
        if not snapshot:
            raise LookupError("房间不存在。")
        state = checkpoint_state if isinstance(checkpoint_state, dict) else {}
        try:
            checkpoint_version = int(state.get("version") or 0)
        except (TypeError, ValueError):
            checkpoint_version = 0

        all_members = list(snapshot.get("members") or [])
        current_by_id = {
            str(member.get("id") or ""): member
            for member in all_members
        }
        approved_member_routes: dict[str, dict[str, Any]] = {}
        if ledger is not None:
            try:
                provider_execution = ledger.snapshot()
            except Exception:
                raise ValueError(
                    "resume Provider-call authorization is invalid"
                ) from None
            if (
                provider_execution.get("member_routes_present") is True
                and provider_execution.get("member_routes_integrity_ok") is not True
            ):
                raise ValueError(
                    "resume Provider member-route authorization is invalid"
                )
            route_manifest = provider_execution.get("member_routes")
            raw_routes = (
                route_manifest.get("members")
                if isinstance(route_manifest, dict)
                and route_manifest.get("version")
                in {"provider_member_routes_v1", "provider_member_routes_v2"}
                else []
            )
            if isinstance(raw_routes, list):
                approved_member_routes = {
                    str(route.get("member_id") or ""): dict(route)
                    for route in raw_routes
                    if isinstance(route, dict)
                    and str(route.get("member_id") or "")
                }
            if raw_routes and len(approved_member_routes) != len(raw_routes):
                raise ValueError(
                    "resume Provider member-route authorization is invalid"
                )
        frozen_moderator: dict[str, Any] | None = None
        route_source = "legacy_current_member"

        if checkpoint_version >= 7:
            required_route_fields = {
                "discussion_mode",
                "domain",
                "moderator_member_id",
                "moderator_member_version",
                "moderator_provider",
                "moderator_model",
            }
            moderator_member_id = str(
                state.get("moderator_member_id") or ""
            ).strip()
            try:
                moderator_member_version = int(
                    state.get("moderator_member_version") or 0
                )
            except (TypeError, ValueError):
                moderator_member_version = 0
            moderator_provider = str(
                state.get("moderator_provider") or ""
            ).strip().lower()
            moderator_model = str(state.get("moderator_model") or "").strip()
            if not required_route_fields.issubset(state):
                return self._blocked_resume_preflight(
                    room_id,
                    checkpoint_version=checkpoint_version,
                    moderator_member_id=moderator_member_id,
                    moderator_member_version=moderator_member_version,
                    provider=moderator_provider,
                    model=moderator_model,
                    error_code="checkpoint_moderator_route_incomplete",
                    message="暂停轮次缺少完整的冻结主持路由。",
                )
            current_moderator = current_by_id.get(moderator_member_id)
            if not current_moderator or not current_moderator.get("enabled"):
                return self._blocked_resume_preflight(
                    room_id,
                    checkpoint_version=checkpoint_version,
                    moderator_member_id=moderator_member_id,
                    moderator_member_version=moderator_member_version,
                    provider=moderator_provider,
                    model=moderator_model,
                    error_code="moderator_disabled",
                    message="暂停轮次冻结的主持成员当前不可用。",
                )
            try:
                frozen_moderator = self.store.get_member_version(
                    room_id,
                    moderator_member_id,
                    moderator_member_version,
                )
            except (TypeError, ValueError):
                frozen_moderator = None
            approved_moderator_route = approved_member_routes.get(
                moderator_member_id
            )
            frozen_moderator_model = str(
                (frozen_moderator or {}).get("model") or ""
            ).strip()
            moderator_route_invalid = bool(
                not frozen_moderator
                or str(frozen_moderator.get("provider") or "").strip().lower()
                != moderator_provider
                or frozen_moderator_model != moderator_model
            )
            if approved_moderator_route:
                moderator_route_invalid = bool(
                    not frozen_moderator
                    or int(
                        approved_moderator_route.get(
                            "approved_member_version"
                        ) or 0
                    )
                    != moderator_member_version
                    or str(approved_moderator_route.get("provider") or "")
                    .strip()
                    .lower()
                    != moderator_provider
                    or str(approved_moderator_route.get("model") or "").strip()
                    != moderator_model
                    or str(frozen_moderator.get("provider") or "")
                    .strip()
                    .lower()
                    != moderator_provider
                    or (
                        frozen_moderator_model
                        and frozen_moderator_model != moderator_model
                    )
                )
            elif approved_member_routes:
                moderator_route_invalid = True
            if moderator_route_invalid:
                return self._blocked_resume_preflight(
                    room_id,
                    checkpoint_version=checkpoint_version,
                    moderator_member_id=moderator_member_id,
                    moderator_member_version=moderator_member_version,
                    provider=moderator_provider,
                    model=moderator_model,
                    error_code="checkpoint_moderator_route_invalid",
                    message="暂停轮次冻结的主持路由与身份版本不一致。",
                )
            moderator_assignment = {
                **frozen_moderator,
                "id": moderator_member_id,
                "provider": moderator_provider,
                "model": moderator_model,
            }
            route_source = (
                "frozen_checkpoint_and_round_ledger"
                if approved_moderator_route
                else "frozen_checkpoint"
            )
        else:
            frozen_member_ids = state.get("member_ids")
            ordered_members = [
                current_by_id[member_id]
                for member_id in (
                    str(item or "").strip()
                    for item in (
                        frozen_member_ids if isinstance(frozen_member_ids, list) else []
                    )
                )
                if member_id in current_by_id
                and current_by_id[member_id].get("enabled")
            ]
            workflow_policy = (
                state.get("workflow_policy")
                if isinstance(state.get("workflow_policy"), dict)
                else {}
            )
            stage_order = list(workflow_policy.get("stage_order") or [])
            first_stage = str(stage_order[0] or "") if stage_order else ""
            moderator_assignment = next(
                (
                    member
                    for member in ordered_members
                    if first_stage
                    and str(member.get("workflow_stage") or "") == first_stage
                ),
                ordered_members[0] if ordered_members else None,
            )
            if not moderator_assignment:
                return self._blocked_resume_preflight(
                    room_id,
                    checkpoint_version=checkpoint_version,
                    moderator_member_id="",
                    moderator_member_version=0,
                    provider="",
                    model="",
                    error_code="moderator_missing",
                    message="暂停轮次没有可恢复的主持成员。",
                )
            moderator_member_id = str(moderator_assignment.get("id") or "")
            moderator_member_version = int(
                moderator_assignment.get("version") or 1
            )
            moderator_provider = str(
                moderator_assignment.get("provider") or ""
            ).strip().lower()
            moderator_model = str(
                moderator_assignment.get("model") or ""
            ).strip()

        # New formal ledgers override only Provider/model. Identity, duties, and
        # boundaries continue to come from the current member version. Legacy
        # ledgers keep the original current-route compatibility path.
        current_routes = (
            self._check_approved_resume_members(
                room_id,
                current_by_id=current_by_id,
                member_ids=member_ids,
                approved_member_routes=approved_member_routes,
                skip_provider_ids=skip_provider_ids,
                ledger=ledger,
            )
            if approved_member_routes and ledger is not None
            else self.check_room(
                room_id,
                member_ids=member_ids,
                skip_provider_ids=skip_provider_ids,
                ledger=ledger,
            )
        )
        moderator_checks = self.providers.preflight(
            [moderator_assignment],
            skip_provider_ids=skip_provider_ids,
            ledger=ledger,
        )
        resolved_moderator_model = self.providers.resolved_model(
            moderator_provider,
            moderator_model,
        )
        moderator_check = next(
            (
                check
                for check in moderator_checks
                if str(check.get("provider") or "") == moderator_provider
                and str(check.get("model") or "") == resolved_moderator_model
            ),
            {},
        )
        moderator_available = bool(moderator_check.get("ready"))
        moderator_state = {
            "id": moderator_member_id,
            "name": str(moderator_assignment.get("name") or ""),
            "available": moderator_available,
            "is_moderator": True,
            "provider": moderator_provider,
            "model": resolved_moderator_model,
            "version": moderator_member_version,
            "route_source": route_source,
            "error_code": str(moderator_check.get("error_code") or ""),
            "message": str(moderator_check.get("message") or ""),
        }

        member_states = [
            {
                **member,
                "is_moderator": str(member.get("id") or "")
                == moderator_member_id,
                "route_source": str(
                    member.get("route_source") or "current_member"
                ),
            }
            for member in current_routes.get("members") or []
        ]
        unavailable_members = [
            {
                **member,
                "is_moderator": str(member.get("id") or "")
                == moderator_member_id,
                "route_source": str(
                    member.get("route_source") or "current_member"
                ),
            }
            for member in current_routes.get("unavailable_members") or []
        ]
        if not moderator_available:
            unavailable_members.append(moderator_state)

        provider_checks_by_key: dict[tuple[str, str], dict[str, Any]] = {}
        provider_check_order: list[tuple[str, str]] = []
        for check in current_routes.get("provider_checks") or []:
            key = (
                str(check.get("provider") or ""),
                str(check.get("model") or ""),
            )
            provider_check_order.append(key)
            provider_checks_by_key[key] = {
                **check,
                "assignment_count": int(check.get("member_count") or 0),
                "assignment_kinds": [
                    "approved_round_ledger"
                    if approved_member_routes
                    else "current_member"
                ],
            }
        moderator_key = (moderator_provider, resolved_moderator_model)
        if moderator_key not in provider_checks_by_key:
            provider_check_order.append(moderator_key)
            provider_checks_by_key[moderator_key] = {
                **moderator_check,
                "member_count": 0,
                "member_ids": [],
                "member_names": [],
                "assignment_count": 0,
                "assignment_kinds": [],
            }
        moderator_route_check = provider_checks_by_key[moderator_key]
        moderator_route_check["assignment_count"] = int(
            moderator_route_check.get("assignment_count") or 0
        ) + 1
        moderator_route_check["assignment_kinds"] = list(dict.fromkeys([
            *(moderator_route_check.get("assignment_kinds") or []),
            route_source,
        ]))
        moderator_route_check["moderator_member_id"] = moderator_member_id
        moderator_route_check["moderator_member_name"] = str(
            moderator_assignment.get("name") or ""
        )
        provider_checks = [
            provider_checks_by_key[key]
            for key in provider_check_order
        ]

        ready = (
            bool(member_states)
            and not current_routes.get("unavailable_members")
            and moderator_available
        )
        return {
            "room_id": room_id,
            "checked_at": int(current_routes.get("checked_at") or time.time() * 1000),
            "ready": ready,
            "context": "round_resume",
            "checkpoint_version": checkpoint_version,
            "member_count": len(member_states),
            "provider_check_count": len(provider_checks),
            "provider_checks": provider_checks,
            "members": member_states,
            "moderator": moderator_state,
            "unavailable_members": unavailable_members,
            "blocking": {
                "moderator_unavailable": not moderator_available,
                "unavailable_member_count": len(unavailable_members),
            },
        }

    @staticmethod
    def _blocked_resume_preflight(
        room_id: str,
        *,
        checkpoint_version: int,
        moderator_member_id: str,
        moderator_member_version: int,
        provider: str,
        model: str,
        error_code: str,
        message: str,
    ) -> dict[str, Any]:
        moderator = {
            "id": moderator_member_id,
            "name": "",
            "available": False,
            "is_moderator": True,
            "provider": provider,
            "model": model,
            "version": moderator_member_version,
            "route_source": "frozen_checkpoint",
            "error_code": error_code,
            "message": message,
        }
        return {
            "room_id": room_id,
            "checked_at": int(time.time() * 1000),
            "ready": False,
            "context": "round_resume",
            "checkpoint_version": checkpoint_version,
            "member_count": 0,
            "provider_check_count": 0,
            "provider_checks": [],
            "members": [],
            "moderator": moderator,
            "unavailable_members": [moderator],
            "blocking": {
                "moderator_unavailable": True,
                "unavailable_member_count": 1,
            },
        }
