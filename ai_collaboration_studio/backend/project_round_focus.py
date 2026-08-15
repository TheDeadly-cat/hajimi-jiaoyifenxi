from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

from .decision_lineage import canonical_sha256
from .domain_adapters import (
    DEFAULT_DOMAIN_ADAPTERS,
    DomainAdapterError,
    DomainAdapterRegistry,
)
from .project_readiness import ProjectReadinessService


PROJECT_ROUND_FOCUS_PACK_ID = "project_round_focus"
PROJECT_ROUND_FOCUS_ADAPTER_ID = "project_round_focus"
PROJECT_ROUND_FOCUS_PORT_ID = "core.round.context/v1"
PROJECT_ROUND_FOCUS_CONTRIBUTION_ID = "project_round_focus.room_inspector/v1"
PROJECT_ROUND_FOCUS_ACTION_ID = "project_round_focus.inspect"
PROJECT_ROUND_FOCUS_PREVIEW_VERSION = "project_round_focus_preview_v1"
PROJECT_ROUND_FOCUS_RECORD_VERSION = "project_round_focus_record_v1"
PROJECT_ROUND_FOCUS_PROJECTION_VERSION = "project_round_focus_projection_v1"
PROJECT_ROUND_FOCUS_RESOLUTION_VERSION = "project_round_focus_resolution_v1"
PROJECT_ROUND_FOCUS_AUTHORIZATION_VERSION = "project_round_focus_authorization_v1"
PROJECT_ROUND_FOCUS_INPUT_SEAL_VERSION = "project_round_focus_input_seal_v1"
ROUND_DOMAIN_CONTEXT_VERSION = "round_domain_context_v1"

_SHA256 = re.compile(r"[0-9a-f]{64}")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}")
_PROJECTION_KEYS = {
    "version",
    "state",
    "counts",
    "focus_items",
    "suggested_objective",
    "provider_calls_performed",
    "market_reads_performed",
    "adapter_business_writes_performed",
    "host_lineage_write_required",
    "ranking_produced",
    "winner_claim",
    "approval_produced",
    "member_assignment_produced",
    "workflow_mutation_performed",
    "user_final_decision_required",
    "can_replace_user_decision",
    "arbitrary_code_loading_allowed",
}
_COUNT_KEYS = {
    "structural_gap_count",
    "blocker_count",
    "evidence_gap_count",
    "focus_item_count",
}
_FOCUS_ITEM_KEYS = {
    "sequence_no",
    "category",
    "code",
    "item_key",
    "message",
    "target_capabilities",
}
_PREVIEW_KEYS = {
    "version",
    "integrity_ok",
    "metrics_visible",
    "room_id",
    "artifact_binding",
    "plugin_registry_snapshot_sha256",
    "input_seal_sha256",
    "resolution",
    "state",
    "counts",
    "focus_items",
    "suggested_objective",
    "preview_sha256",
    "provider_calls_performed",
    "market_reads_performed",
    "adapter_business_writes_performed",
    "host_lineage_write_required",
    "execution_capability",
    "live_trading_allowed",
    "can_autonomously_decide",
    "can_replace_user_decision",
    "arbitrary_code_loading_allowed",
    "ranking_produced",
    "winner_claim",
    "approval_produced",
    "member_assignment_produced",
    "workflow_mutation_performed",
    "user_final_decision_required",
}


class ProjectRoundFocusError(ValueError):
    def __init__(self, message: str, *, code: str, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status = int(status)


def normalize_project_round_focus_authorization(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "version",
        "artifact_binding",
        "preview_sha256",
        "user_confirmed",
    }:
        raise ProjectRoundFocusError(
            "Project round focus authorization has an invalid closed shape.",
            code="ROUND_FOCUS_AUTHORIZATION_INVALID",
            status=400,
        )
    if value.get("version") != PROJECT_ROUND_FOCUS_AUTHORIZATION_VERSION:
        raise ProjectRoundFocusError(
            "Project round focus authorization version is unsupported.",
            code="ROUND_FOCUS_AUTHORIZATION_INVALID",
            status=400,
        )
    if value.get("user_confirmed") is not True:
        raise ProjectRoundFocusError(
            "The user must confirm the exact next-round focus preview.",
            code="ROUND_FOCUS_AUTHORIZATION_REQUIRED",
            status=400,
        )
    preview_sha256 = str(value.get("preview_sha256") or "").strip().lower()
    if not _SHA256.fullmatch(preview_sha256):
        raise ProjectRoundFocusError(
            "Project round focus preview seal is invalid.",
            code="ROUND_FOCUS_AUTHORIZATION_INVALID",
            status=400,
        )
    binding = value.get("artifact_binding")
    if not isinstance(binding, dict):
        raise ProjectRoundFocusError(
            "Project round focus artifact binding is invalid.",
            code="ROUND_FOCUS_AUTHORIZATION_INVALID",
            status=400,
        )
    status = str(binding.get("status") or "")
    if status == "none" and set(binding) == {"status"}:
        clean_binding = {"status": "none"}
    elif status == "exact" and set(binding) == {
        "status",
        "artifact_id",
        "artifact_version",
    }:
        artifact_id = str(binding.get("artifact_id") or "").strip()
        artifact_version = binding.get("artifact_version")
        if (
            not _ID.fullmatch(artifact_id)
            or isinstance(artifact_version, bool)
            or not isinstance(artifact_version, int)
            or artifact_version <= 0
            or artifact_version > 2**63 - 1
        ):
            raise ProjectRoundFocusError(
                "Project round focus exact artifact binding is invalid.",
                code="ROUND_FOCUS_AUTHORIZATION_INVALID",
                status=400,
            )
        clean_binding = {
            "status": "exact",
            "artifact_id": artifact_id,
            "artifact_version": artifact_version,
        }
    else:
        raise ProjectRoundFocusError(
            "Project round focus artifact binding is invalid.",
            code="ROUND_FOCUS_AUTHORIZATION_INVALID",
            status=400,
        )
    return {
        "version": PROJECT_ROUND_FOCUS_AUTHORIZATION_VERSION,
        "artifact_binding": clean_binding,
        "preview_sha256": preview_sha256,
        "user_confirmed": True,
    }


class ProjectRoundFocusService:
    def __init__(
        self,
        store: Any,
        domain_adapters: DomainAdapterRegistry = DEFAULT_DOMAIN_ADAPTERS,
    ) -> None:
        self.store = store
        self.domain_adapters = domain_adapters

    @staticmethod
    def _invalid(message: str) -> ProjectRoundFocusError:
        return ProjectRoundFocusError(
            message,
            code="PROJECT_ROUND_FOCUS_OUTPUT_INVALID",
            status=409,
        )

    @classmethod
    def _validate_projection(cls, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict) or set(value) != _PROJECTION_KEYS:
            raise cls._invalid("The round-focus adapter output has an invalid closed shape.")
        if value.get("version") != PROJECT_ROUND_FOCUS_PROJECTION_VERSION:
            raise cls._invalid("The round-focus adapter output version is invalid.")
        if value.get("state") not in {"bootstrap", "ready", "gaps_present", "blocked"}:
            raise cls._invalid("The round-focus adapter state is invalid.")
        counts = value.get("counts")
        if not isinstance(counts, dict) or set(counts) != _COUNT_KEYS or any(
            isinstance(counts.get(key), bool)
            or not isinstance(counts.get(key), int)
            or not 0 <= counts[key] <= 16
            for key in _COUNT_KEYS
        ):
            raise cls._invalid("The round-focus counts are invalid.")
        rows = value.get("focus_items")
        if not isinstance(rows, list) or len(rows) > 16:
            raise cls._invalid("The round-focus item list is invalid.")
        category_counts = {"structural": 0, "evidence": 0, "blocker": 0}
        clean_rows: list[dict[str, Any]] = []
        for index, row in enumerate(rows, 1):
            if not isinstance(row, dict) or set(row) != _FOCUS_ITEM_KEYS:
                raise cls._invalid("A round-focus item has an invalid closed shape.")
            category = row.get("category")
            capabilities = row.get("target_capabilities")
            if (
                row.get("sequence_no") != index
                or category not in category_counts
                or not isinstance(capabilities, list)
                or not capabilities
                or len(capabilities) > 4
                or any(not isinstance(item, str) or not item for item in capabilities)
                or any(
                    not isinstance(row.get(field), str)
                    or not str(row.get(field) or "").strip()
                    for field in ("code", "item_key", "message")
                )
            ):
                raise cls._invalid("A round-focus item is invalid.")
            category_counts[str(category)] += 1
            clean_rows.append(deepcopy(row))
        expected_counts = {
            "structural_gap_count": category_counts["structural"],
            "blocker_count": category_counts["blocker"],
            "evidence_gap_count": category_counts["evidence"],
            "focus_item_count": len(clean_rows),
        }
        if counts != expected_counts:
            raise cls._invalid("The round-focus counts do not match the item list.")
        suggested_objective = value.get("suggested_objective")
        if (
            not isinstance(suggested_objective, str)
            or not suggested_objective.strip()
            or len(suggested_objective) > 4000
        ):
            raise cls._invalid("The suggested round objective is invalid.")
        fixed = {
            "provider_calls_performed": 0,
            "market_reads_performed": 0,
            "adapter_business_writes_performed": 0,
            "host_lineage_write_required": True,
            "ranking_produced": False,
            "winner_claim": False,
            "approval_produced": False,
            "member_assignment_produced": False,
            "workflow_mutation_performed": False,
            "user_final_decision_required": True,
            "can_replace_user_decision": False,
            "arbitrary_code_loading_allowed": False,
        }
        if any(value.get(key) != expected for key, expected in fixed.items()):
            raise cls._invalid("The round-focus adapter safety fields are invalid.")
        return deepcopy(value)

    @staticmethod
    def _public_port(port_resolution: dict[str, Any]) -> dict[str, Any]:
        return {
            "port_id": str(port_resolution.get("port_id") or ""),
            "port_version": str(port_resolution.get("port_version") or ""),
            "contract_sha256": str(port_resolution.get("port_contract_sha256") or ""),
            "input_schema_version": str(port_resolution.get("input_schema_version") or ""),
            "input_schema_sha256": str(port_resolution.get("input_schema_sha256") or ""),
            "output_schema_version": str(port_resolution.get("output_schema_version") or ""),
            "output_schema_sha256": str(port_resolution.get("output_schema_sha256") or ""),
            "provider_call_budget": int(port_resolution.get("provider_call_budget") or 0),
            "market_read_budget": int(port_resolution.get("market_read_budget") or 0),
            "business_write_budget": int(port_resolution.get("business_write_budget") or 0),
            "failure_policy": str(port_resolution.get("failure_policy") or ""),
        }

    @classmethod
    def _expected_semantics(
        cls,
        artifact_binding: dict[str, Any],
        readiness: dict[str, Any] | None,
        room_context: dict[str, Any],
    ) -> dict[str, Any]:
        objective = str(room_context.get("objective") or "").strip()[:4000]
        if artifact_binding.get("status") == "none":
            if readiness is not None:
                raise cls._invalid("Bootstrap focus unexpectedly contains readiness data.")
            return {
                "state": "bootstrap",
                "counts": {
                    "structural_gap_count": 0,
                    "blocker_count": 0,
                    "evidence_gap_count": 0,
                    "focus_item_count": 0,
                },
                "focus_items": [],
                "suggested_objective": objective,
            }
        if not isinstance(readiness, dict):
            raise cls._invalid("Exact focus is missing readiness data.")
        state = readiness.get("state")
        if state not in {"ready", "gaps_present", "blocked"}:
            raise cls._invalid("Readiness state is invalid for round focus.")
        groups = (
            ("blocker", readiness.get("blockers"), ["critical_review"]),
            ("evidence", readiness.get("evidence_gaps"), ["evidence_review"]),
            (
                "structural",
                readiness.get("structural_gaps"),
                ["evidence_review", "decision_synthesis"],
            ),
        )
        focus_items: list[dict[str, Any]] = []
        for category, rows, capabilities in groups:
            if not isinstance(rows, list):
                raise cls._invalid("Readiness gap rows are invalid for round focus.")
            for row in rows:
                if not isinstance(row, dict) or set(row) != {
                    "code",
                    "item_key",
                    "message",
                }:
                    raise cls._invalid("A readiness gap row has an invalid shape.")
                code = str(row.get("code") or "").strip()[:80]
                item_key = str(row.get("item_key") or "").strip()[:180]
                message = str(row.get("message") or "").strip()[:500]
                if not code or not item_key or not message:
                    raise cls._invalid("A readiness gap row is incomplete.")
                if len(focus_items) < 16:
                    focus_items.append({
                        "sequence_no": len(focus_items) + 1,
                        "category": category,
                        "code": code,
                        "item_key": item_key,
                        "message": message,
                        "target_capabilities": list(capabilities),
                    })
        counts = {
            "structural_gap_count": sum(
                row["category"] == "structural" for row in focus_items
            ),
            "blocker_count": sum(
                row["category"] == "blocker" for row in focus_items
            ),
            "evidence_gap_count": sum(
                row["category"] == "evidence" for row in focus_items
            ),
            "focus_item_count": len(focus_items),
        }
        suggested_objective = objective
        if focus_items:
            summary = "; ".join(
                f"{row['code']} ({row['item_key']})" for row in focus_items[:8]
            )
            suggested_objective = ("补齐已冻结的项目缺口：" + summary)[:4000]
        return {
            "state": state,
            "counts": counts,
            "focus_items": focus_items,
            "suggested_objective": suggested_objective,
        }

    def _build_from_inputs(
        self,
        room_id: str,
        inputs: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        raw_artifact_binding = (
            inputs.get("artifact_binding")
            if isinstance(inputs.get("artifact_binding"), dict)
            else {}
        )
        artifact_binding = {
            field: raw_artifact_binding.get(field)
            for field in (
                "status",
                "artifact_id",
                "artifact_title",
                "artifact_version",
                "artifact_snapshot_sha256",
                "evidence_review_event_sha256",
                "evidence_graph_sha256",
            )
        }
        if artifact_binding.get("status") == "exact":
            readiness_inputs = inputs.get("project_readiness_inputs")
            if not isinstance(readiness_inputs, dict):
                raise ProjectRoundFocusError(
                    "The exact readiness input snapshot is unavailable.",
                    code="PROJECT_ROUND_FOCUS_SOURCE_INVALID",
                )
            readiness = ProjectReadinessService(
                self.store,
                self.domain_adapters,
            ).inspect_from_inputs(
                room_id,
                str(artifact_binding.get("artifact_id") or ""),
                int(artifact_binding.get("artifact_version") or 0),
                readiness_inputs,
            )
            if (
                str(readiness.get("artifact_snapshot_sha256") or "")
                != str(artifact_binding.get("artifact_snapshot_sha256") or "")
                or str(readiness.get("evidence_graph_sha256") or "")
                != str(artifact_binding.get("evidence_graph_sha256") or "")
            ):
                raise ProjectRoundFocusError(
                    "The project readiness source drifted while building the preview.",
                    code="PROJECT_ROUND_FOCUS_SOURCE_DRIFT",
                )
        else:
            readiness = None
        binding = inputs.get("binding") if isinstance(inputs.get("binding"), dict) else {}
        port_resolution = (
            binding.get("port_resolution")
            if isinstance(binding.get("port_resolution"), dict)
            else {}
        )
        try:
            adapter = self.domain_adapters.require_port_resolution(port_resolution)
            handler = getattr(adapter, "project_round_context", None)
            if not callable(handler):
                raise DomainAdapterError("round-context handler is unavailable")
            projection = handler(
                artifact_binding=deepcopy(artifact_binding),
                readiness_projection=deepcopy(readiness),
                room_context=deepcopy(inputs.get("room_context") or {}),
            )
        except DomainAdapterError as exc:
            raise ProjectRoundFocusError(
                "The exact project round-focus implementation is unavailable.",
                code="PROJECT_ROUND_FOCUS_IMPLEMENTATION_UNAVAILABLE",
            ) from exc
        projection = self._validate_projection(projection)
        expected_semantics = self._expected_semantics(
            artifact_binding,
            readiness,
            inputs.get("room_context")
            if isinstance(inputs.get("room_context"), dict)
            else {},
        )
        if any(
            projection.get(field) != expected_semantics[field]
            for field in (
                "state",
                "counts",
                "focus_items",
                "suggested_objective",
            )
        ):
            raise self._invalid(
                "The round-focus adapter output does not match the sealed readiness input."
            )
        input_seal = deepcopy(inputs.get("input_seal") or {})
        input_seal_sha256 = canonical_sha256(input_seal)
        contribution = (
            binding.get("contribution")
            if isinstance(binding.get("contribution"), dict)
            else {}
        )
        adapter_ref = (
            binding.get("adapter")
            if isinstance(binding.get("adapter"), dict)
            else {}
        )
        public = {
            "version": PROJECT_ROUND_FOCUS_PREVIEW_VERSION,
            "integrity_ok": True,
            "metrics_visible": True,
            "room_id": str(room_id),
            "artifact_binding": artifact_binding,
            "plugin_registry_snapshot_sha256": str(
                binding.get("plugin_registry_snapshot_sha256") or ""
            ),
            "input_seal_sha256": input_seal_sha256,
            "resolution": {
                "version": PROJECT_ROUND_FOCUS_RESOLUTION_VERSION,
                "contribution": {
                    field: contribution.get(field)
                    for field in (
                        "contribution_id",
                        "contribution_version",
                        "contract_sha256",
                        "slot_id",
                        "component_key",
                    )
                },
                "adapter": {
                    field: adapter_ref.get(field)
                    for field in (
                        "adapter_id",
                        "adapter_version",
                        "contract_version",
                        "contract_sha256",
                    )
                },
                "port": self._public_port(port_resolution),
            },
            "state": projection["state"],
            "counts": deepcopy(projection["counts"]),
            "focus_items": deepcopy(projection["focus_items"]),
            "suggested_objective": projection["suggested_objective"],
            "provider_calls_performed": 0,
            "market_reads_performed": 0,
            "adapter_business_writes_performed": 0,
            "host_lineage_write_required": True,
            "execution_capability": "none",
            "live_trading_allowed": False,
            "can_autonomously_decide": False,
            "can_replace_user_decision": False,
            "arbitrary_code_loading_allowed": False,
            "ranking_produced": False,
            "winner_claim": False,
            "approval_produced": False,
            "member_assignment_produced": False,
            "workflow_mutation_performed": False,
            "user_final_decision_required": True,
        }
        public["preview_sha256"] = canonical_sha256(public)
        if set(public) != _PREVIEW_KEYS:
            raise self._invalid("The public round-focus preview has an invalid shape.")
        return public, inputs

    def _build(self, room_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        return self._build_from_inputs(
            room_id,
            self.store.project_round_focus_inputs(room_id),
        )

    def preview(self, room_id: str) -> dict[str, Any]:
        preview, _ = self._build(room_id)
        return preview

    def prepare_authorized(
        self,
        room_id: str,
        authorization: Any,
    ) -> dict[str, Any]:
        clean_authorization = normalize_project_round_focus_authorization(authorization)
        preview, inputs = self._build(room_id)
        expected_binding = clean_authorization["artifact_binding"]
        public_binding = preview["artifact_binding"]
        if expected_binding["status"] == "none":
            binding_matches = public_binding.get("status") == "none"
        else:
            binding_matches = (
                public_binding.get("status") == "exact"
                and public_binding.get("artifact_id") == expected_binding["artifact_id"]
                and public_binding.get("artifact_version")
                == expected_binding["artifact_version"]
            )
        if (
            not binding_matches
            or preview["preview_sha256"] != clean_authorization["preview_sha256"]
        ):
            raise ProjectRoundFocusError(
                "The project round-focus preview changed after user confirmation.",
                code="PROJECT_ROUND_FOCUS_PREVIEW_DRIFT",
            )
        input_seal = (
            deepcopy(inputs.get("input_seal"))
            if isinstance(inputs.get("input_seal"), dict)
            else {}
        )
        input_seal_sha256 = canonical_sha256(input_seal)
        if input_seal_sha256 != preview.get("input_seal_sha256"):
            raise ProjectRoundFocusError(
                "The project round-focus input seal is inconsistent.",
                code="PROJECT_ROUND_FOCUS_INPUT_INTEGRITY_FAILED",
            )
        return {
            "version": ROUND_DOMAIN_CONTEXT_VERSION,
            "authorization": clean_authorization,
            "input_seal": input_seal,
            "input_seal_sha256": canonical_sha256(input_seal),
            "preview": preview,
            "preview_sha256": preview["preview_sha256"],
            "output_sha256": canonical_sha256(preview),
        }

    @staticmethod
    def legacy_workspace_from_preview(value: Any) -> dict[str, Any]:
        """Project the frozen focus into the existing prompt/checkpoint shape."""

        preview = validate_project_round_focus_preview(value)
        artifact = preview["artifact_binding"]
        gaps = [
            {
                "code": row["code"],
                "title": row["message"],
                "detail": row["item_key"],
                "target_capabilities": deepcopy(row["target_capabilities"]),
            }
            for row in preview["focus_items"]
        ]
        counts = preview["counts"]
        return {
            "applicable": True,
            "frozen": True,
            "artifact_id": artifact["artifact_id"],
            "artifact_version": artifact["artifact_version"],
            "artifact_status": (
                "CONFIRMED" if artifact["status"] == "exact" else "NONE"
            ),
            "requirement_count": counts["structural_gap_count"],
            "confirmed_requirement_count": 0,
            "pending_requirement_count": counts["structural_gap_count"],
            "missing_acceptance_count": counts["structural_gap_count"],
            "risk_count": counts["blocker_count"],
            "blocking_open_risk_count": counts["blocker_count"],
            "missing_risk_trigger_count": 0,
            "untreated_risk_count": counts["blocker_count"],
            "option_count": 0,
            "matrix_missing_dimensions": [],
            "preferred_option_ready": False,
            "gaps": gaps,
            "focus": deepcopy(gaps[0]) if gaps else None,
            "ready": preview["state"] in {"ready", "bootstrap"},
            "label": preview["suggested_objective"],
            "project_round_focus_preview_sha256": preview["preview_sha256"],
            "project_round_focus_input_seal_sha256": preview[
                "input_seal_sha256"
            ],
        }


def validate_project_round_focus_preview(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _PREVIEW_KEYS:
        raise ProjectRoundFocusError(
            "Project round-focus preview has an invalid closed shape.",
            code="PROJECT_ROUND_FOCUS_INTEGRITY_FAILED",
        )
    preview = deepcopy(value)
    stored_sha256 = str(preview.pop("preview_sha256", "") or "")
    if (
        value.get("version") != PROJECT_ROUND_FOCUS_PREVIEW_VERSION
        or not _SHA256.fullmatch(stored_sha256)
        or canonical_sha256(preview) != stored_sha256
        or not _SHA256.fullmatch(
            str(value.get("plugin_registry_snapshot_sha256") or "")
        )
        or not _SHA256.fullmatch(str(value.get("input_seal_sha256") or ""))
        or value.get("integrity_ok") is not True
        or value.get("metrics_visible") is not True
    ):
        raise ProjectRoundFocusError(
            "Project round-focus preview seal is invalid.",
            code="PROJECT_ROUND_FOCUS_INTEGRITY_FAILED",
        )
    artifact = value.get("artifact_binding")
    if not isinstance(artifact, dict) or set(artifact) != {
        "status",
        "artifact_id",
        "artifact_title",
        "artifact_version",
        "artifact_snapshot_sha256",
        "evidence_review_event_sha256",
        "evidence_graph_sha256",
    }:
        raise ProjectRoundFocusError(
            "Project round-focus artifact binding is invalid.",
            code="PROJECT_ROUND_FOCUS_INTEGRITY_FAILED",
        )
    artifact_version = artifact.get("artifact_version")
    if artifact.get("status") == "none":
        if any(
            artifact.get(field) not in {"", 0}
            for field in (
                "artifact_id",
                "artifact_title",
                "artifact_version",
                "artifact_snapshot_sha256",
                "evidence_review_event_sha256",
                "evidence_graph_sha256",
            )
        ) or value.get("state") != "bootstrap":
            raise ProjectRoundFocusError(
                "Bootstrap artifact binding is inconsistent.",
                code="PROJECT_ROUND_FOCUS_INTEGRITY_FAILED",
            )
    elif artifact.get("status") == "exact":
        if (
            not _ID.fullmatch(str(artifact.get("artifact_id") or ""))
            or isinstance(artifact_version, bool)
            or not isinstance(artifact_version, int)
            or not 0 < artifact_version <= 2**63 - 1
            or not isinstance(artifact.get("artifact_title"), str)
            or len(str(artifact.get("artifact_title") or "")) > 240
            or any(
                not _SHA256.fullmatch(str(artifact.get(field) or ""))
                for field in (
                    "artifact_snapshot_sha256",
                    "evidence_review_event_sha256",
                    "evidence_graph_sha256",
                )
            )
            or value.get("state") == "bootstrap"
        ):
            raise ProjectRoundFocusError(
                "Exact artifact binding is inconsistent.",
                code="PROJECT_ROUND_FOCUS_INTEGRITY_FAILED",
            )
    else:
        raise ProjectRoundFocusError(
            "Project round-focus artifact status is invalid.",
            code="PROJECT_ROUND_FOCUS_INTEGRITY_FAILED",
        )
    resolution = value.get("resolution")
    if not isinstance(resolution, dict) or set(resolution) != {
        "version",
        "contribution",
        "adapter",
        "port",
    } or resolution.get("version") != PROJECT_ROUND_FOCUS_RESOLUTION_VERSION:
        raise ProjectRoundFocusError(
            "Project round-focus resolution is invalid.",
            code="PROJECT_ROUND_FOCUS_INTEGRITY_FAILED",
        )
    contribution = resolution.get("contribution")
    adapter = resolution.get("adapter")
    port = resolution.get("port")
    if (
        not isinstance(contribution, dict)
        or set(contribution) != {
            "contribution_id",
            "contribution_version",
            "contract_sha256",
            "slot_id",
            "component_key",
        }
        or contribution.get("contribution_id")
        != PROJECT_ROUND_FOCUS_CONTRIBUTION_ID
        or not _SHA256.fullmatch(str(contribution.get("contract_sha256") or ""))
        or not isinstance(adapter, dict)
        or set(adapter) != {
            "adapter_id",
            "adapter_version",
            "contract_version",
            "contract_sha256",
        }
        or adapter.get("adapter_id") != PROJECT_ROUND_FOCUS_ADAPTER_ID
        or not _SHA256.fullmatch(str(adapter.get("contract_sha256") or ""))
        or not isinstance(port, dict)
        or set(port) != {
            "port_id",
            "port_version",
            "contract_sha256",
            "input_schema_version",
            "input_schema_sha256",
            "output_schema_version",
            "output_schema_sha256",
            "provider_call_budget",
            "market_read_budget",
            "business_write_budget",
            "failure_policy",
        }
        or port.get("port_id") != PROJECT_ROUND_FOCUS_PORT_ID
        or any(
            not _SHA256.fullmatch(str(port.get(field) or ""))
            for field in (
                "contract_sha256",
                "input_schema_sha256",
                "output_schema_sha256",
            )
        )
        or any(
            port.get(field) != 0
            for field in (
                "provider_call_budget",
                "market_read_budget",
                "business_write_budget",
            )
        )
        or port.get("failure_policy") != "fail_closed"
    ):
        raise ProjectRoundFocusError(
            "Project round-focus exact port resolution is invalid.",
            code="PROJECT_ROUND_FOCUS_INTEGRITY_FAILED",
        )
    projection = {
        "version": PROJECT_ROUND_FOCUS_PROJECTION_VERSION,
        "state": value.get("state"),
        "counts": deepcopy(value.get("counts")),
        "focus_items": deepcopy(value.get("focus_items")),
        "suggested_objective": value.get("suggested_objective"),
        "provider_calls_performed": value.get("provider_calls_performed"),
        "market_reads_performed": value.get("market_reads_performed"),
        "adapter_business_writes_performed": value.get(
            "adapter_business_writes_performed"
        ),
        "host_lineage_write_required": value.get("host_lineage_write_required"),
        "ranking_produced": value.get("ranking_produced"),
        "winner_claim": value.get("winner_claim"),
        "approval_produced": value.get("approval_produced"),
        "member_assignment_produced": value.get("member_assignment_produced"),
        "workflow_mutation_performed": value.get("workflow_mutation_performed"),
        "user_final_decision_required": value.get("user_final_decision_required"),
        "can_replace_user_decision": value.get("can_replace_user_decision"),
        "arbitrary_code_loading_allowed": value.get(
            "arbitrary_code_loading_allowed"
        ),
    }
    ProjectRoundFocusService._validate_projection(projection)
    fixed_public = {
        "execution_capability": "none",
        "live_trading_allowed": False,
        "can_autonomously_decide": False,
        "can_replace_user_decision": False,
        "arbitrary_code_loading_allowed": False,
        "ranking_produced": False,
        "winner_claim": False,
        "approval_produced": False,
        "member_assignment_produced": False,
        "workflow_mutation_performed": False,
        "user_final_decision_required": True,
        "provider_calls_performed": 0,
        "market_reads_performed": 0,
        "adapter_business_writes_performed": 0,
        "host_lineage_write_required": True,
    }
    if any(value.get(key) != expected for key, expected in fixed_public.items()):
        raise ProjectRoundFocusError(
            "Project round-focus public safety projection is invalid.",
            code="PROJECT_ROUND_FOCUS_INTEGRITY_FAILED",
        )
    return deepcopy(value)


__all__ = [
    "PROJECT_ROUND_FOCUS_ACTION_ID",
    "PROJECT_ROUND_FOCUS_ADAPTER_ID",
    "PROJECT_ROUND_FOCUS_AUTHORIZATION_VERSION",
    "PROJECT_ROUND_FOCUS_CONTRIBUTION_ID",
    "PROJECT_ROUND_FOCUS_INPUT_SEAL_VERSION",
    "PROJECT_ROUND_FOCUS_PACK_ID",
    "PROJECT_ROUND_FOCUS_PORT_ID",
    "PROJECT_ROUND_FOCUS_PREVIEW_VERSION",
    "PROJECT_ROUND_FOCUS_RECORD_VERSION",
    "PROJECT_ROUND_FOCUS_RESOLUTION_VERSION",
    "ROUND_DOMAIN_CONTEXT_VERSION",
    "ProjectRoundFocusError",
    "ProjectRoundFocusService",
    "normalize_project_round_focus_authorization",
    "validate_project_round_focus_preview",
]
