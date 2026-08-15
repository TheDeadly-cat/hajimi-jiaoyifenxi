from __future__ import annotations

from copy import deepcopy
import re
from typing import Any, TYPE_CHECKING

from .domain_adapters import (
    DEFAULT_DOMAIN_ADAPTERS,
    DomainAdapterError,
    DomainAdapterRegistry,
)

if TYPE_CHECKING:  # pragma: no cover
    from .store import StudioStore


PROJECT_READINESS_ACTION_ID = "project_readiness.inspect"
PROJECT_READINESS_ADAPTER_ID = "project_readiness"
PROJECT_READINESS_CONTRIBUTION_ID = "project_readiness.artifact_workspace/v1"
PROJECT_READINESS_PORT_ID = "core.artifact.projection/v1"
PROJECT_READINESS_RESPONSE_VERSION = "project_readiness_response_v1"

_PROJECT_READINESS_OUTPUT_KEYS = {
    "version",
    "state",
    "requirement_gaps",
    "evidence_gaps",
    "risk_gaps",
    "blockers",
    "counts",
    "provider_calls_performed",
    "market_reads_performed",
    "business_writes_performed",
    "ranking_produced",
    "winner_claim",
    "approval_produced",
    "user_final_decision_required",
    "can_replace_user_decision",
    "arbitrary_code_loading_allowed",
}
_PROJECT_READINESS_COUNT_KEYS = {
    "requirement_gap_count",
    "evidence_gap_count",
    "risk_gap_count",
    "blocker_count",
}
_REQUIREMENT_GAP_CODES = frozenset({
    "REQUIREMENT_TEXT_MISSING",
    "REQUIREMENT_NOT_CONFIRMED",
    "REQUIREMENT_OWNER_MISSING",
    "REQUIREMENT_ACCEPTANCE_CRITERIA_MISSING",
})
_RISK_GAP_CODES = frozenset({
    "RISK_TRIGGER_MISSING",
    "RISK_MITIGATION_MISSING",
    "RISK_OWNER_MISSING",
})
_EVIDENCE_GAP_CODES = frozenset({
    "EVIDENCE_RELATION_MISSING",
    "EVIDENCE_UNREVIEWED",
    "EVIDENCE_DISPUTED",
})
_BLOCKER_CODES = frozenset({
    "BLOCKING_RISK_UNRESOLVED",
    "BLOCKING_DISAGREEMENT_UNRESOLVED",
})
_OUTPUT_CODE_PATTERN = re.compile(r"[A-Z][A-Z0-9_]{0,79}")


class ProjectReadinessError(ValueError):
    def __init__(self, message: str, *, code: str, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class ProjectReadinessService:
    """Read-only broker for one sealed artifact projection port invocation."""

    def __init__(
        self,
        store: "StudioStore",
        domain_adapters: DomainAdapterRegistry = DEFAULT_DOMAIN_ADAPTERS,
    ) -> None:
        self.store = store
        self.domain_adapters = domain_adapters

    @staticmethod
    def _output_invalid(message: str) -> ProjectReadinessError:
        return ProjectReadinessError(
            message,
            code="PROJECT_READINESS_OUTPUT_INVALID",
        )

    @classmethod
    def _validate_gap_rows(
        cls,
        value: Any,
        *,
        field: str,
        allowed_codes: frozenset[str],
        include_item_key: bool = False,
    ) -> list[dict[str, Any]]:
        if not isinstance(value, list) or len(value) > 500:
            raise cls._output_invalid(f"{field} must be a bounded array.")
        expected_keys = {"item_id", "codes"}
        if include_item_key:
            expected_keys.add("item_key")
        rows: list[dict[str, Any]] = []
        for index, row in enumerate(value):
            if not isinstance(row, dict) or set(row) != expected_keys:
                raise cls._output_invalid(f"{field}[{index}] has an invalid shape.")
            item_id = row.get("item_id")
            if not isinstance(item_id, str) or not item_id or len(item_id) > 80:
                raise cls._output_invalid(f"{field}[{index}].item_id is invalid.")
            if include_item_key:
                item_key = row.get("item_key")
                if (
                    not isinstance(item_key, str)
                    or not item_key
                    or len(item_key) > 180
                ):
                    raise cls._output_invalid(f"{field}[{index}].item_key is invalid.")
            codes = row.get("codes")
            if (
                not isinstance(codes, list)
                or not 1 <= len(codes) <= 16
                or len(codes) != len(set(codes))
                or any(
                    not isinstance(code, str)
                    or not _OUTPUT_CODE_PATTERN.fullmatch(code)
                    or code not in allowed_codes
                    for code in codes
                )
            ):
                raise cls._output_invalid(f"{field}[{index}].codes is invalid.")
            rows.append(row)
        return rows

    @classmethod
    def _validate_blockers(cls, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list) or len(value) > 500:
            raise cls._output_invalid("blockers must be a bounded array.")
        rows: list[dict[str, Any]] = []
        for index, row in enumerate(value):
            if not isinstance(row, dict) or set(row) != {"item_id", "code"}:
                raise cls._output_invalid(f"blockers[{index}] has an invalid shape.")
            item_id = row.get("item_id")
            code = row.get("code")
            if (
                not isinstance(item_id, str)
                or not item_id
                or len(item_id) > 80
                or not isinstance(code, str)
                or code not in _BLOCKER_CODES
            ):
                raise cls._output_invalid(f"blockers[{index}] is invalid.")
            rows.append(row)
        return rows

    @classmethod
    def _validate_projection(cls, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict) or set(value) != _PROJECT_READINESS_OUTPUT_KEYS:
            raise cls._output_invalid("The adapter output does not match the sealed schema.")
        if value.get("version") != "project_readiness_projection_v1":
            raise cls._output_invalid("The adapter output version is unsupported.")
        if value.get("state") not in {"ready", "gaps_present", "blocked"}:
            raise cls._output_invalid("The adapter output state is invalid.")

        requirement_gaps = cls._validate_gap_rows(
            value.get("requirement_gaps"),
            field="requirement_gaps",
            allowed_codes=_REQUIREMENT_GAP_CODES,
        )
        evidence_gaps = cls._validate_gap_rows(
            value.get("evidence_gaps"),
            field="evidence_gaps",
            allowed_codes=_EVIDENCE_GAP_CODES,
            include_item_key=True,
        )
        risk_gaps = cls._validate_gap_rows(
            value.get("risk_gaps"),
            field="risk_gaps",
            allowed_codes=_RISK_GAP_CODES,
        )
        blockers = cls._validate_blockers(value.get("blockers"))
        counts = value.get("counts")
        expected_counts = {
            "requirement_gap_count": len(requirement_gaps),
            "evidence_gap_count": len(evidence_gaps),
            "risk_gap_count": len(risk_gaps),
            "blocker_count": len(blockers),
        }
        if (
            not isinstance(counts, dict)
            or set(counts) != _PROJECT_READINESS_COUNT_KEYS
            or any(type(counts.get(key)) is not int for key in expected_counts)
            or counts != expected_counts
        ):
            raise cls._output_invalid("The adapter output counts are invalid.")

        for field in (
            "provider_calls_performed",
            "market_reads_performed",
            "business_writes_performed",
        ):
            if type(value.get(field)) is not int or value.get(field) != 0:
                raise cls._output_invalid(f"{field} must be the integer zero.")
        for field, expected in {
            "ranking_produced": False,
            "winner_claim": False,
            "approval_produced": False,
            "user_final_decision_required": True,
            "can_replace_user_decision": False,
            "arbitrary_code_loading_allowed": False,
        }.items():
            if type(value.get(field)) is not bool or value.get(field) is not expected:
                raise cls._output_invalid(f"{field} violates the safety contract.")

        has_gaps = bool(requirement_gaps or evidence_gaps or risk_gaps)
        expected_state = "blocked" if blockers else "gaps_present" if has_gaps else "ready"
        if value.get("state") != expected_state:
            raise cls._output_invalid("The adapter output state is inconsistent.")
        return value

    def inspect(
        self,
        room_id: str,
        artifact_id: str,
        artifact_version: int,
    ) -> dict[str, Any]:
        inputs = self.store.project_readiness_inputs(
            room_id,
            artifact_id,
            artifact_version,
        )
        return self.inspect_from_inputs(
            room_id,
            artifact_id,
            artifact_version,
            inputs,
        )

    def inspect_from_inputs(
        self,
        room_id: str,
        artifact_id: str,
        artifact_version: int,
        inputs: dict[str, Any],
    ) -> dict[str, Any]:
        """Project one already-verified SQLite snapshot without reading again."""

        if not isinstance(inputs, dict):
            raise self._output_invalid("The readiness input snapshot is invalid.")
        binding = inputs.get("binding") if isinstance(inputs.get("binding"), dict) else {}
        port_resolution = (
            binding.get("port_resolution")
            if isinstance(binding.get("port_resolution"), dict)
            else {}
        )
        try:
            adapter = self.domain_adapters.require_port_resolution(port_resolution)
        except DomainAdapterError as exc:
            raise ProjectReadinessError(
                "项目就绪度端口的精确实现不可用。",
                code="PROJECT_READINESS_IMPLEMENTATION_UNAVAILABLE",
            ) from exc
        project = getattr(adapter, "project_artifact", None)
        if not callable(project):
            raise ProjectReadinessError(
                "项目就绪度端口没有可调用实现。",
                code="PROJECT_READINESS_IMPLEMENTATION_UNAVAILABLE",
            )
        try:
            projection = project(
                artifact=deepcopy(inputs.get("artifact") or {}),
                evidence_relations=deepcopy(inputs.get("evidence_relations") or []),
            )
        except ProjectReadinessError:
            raise
        except Exception as exc:
            raise self._output_invalid("The adapter projection failed.") from exc
        projection = self._validate_projection(projection)
        messages = {
            "REQUIREMENT_TEXT_MISSING": "需求缺少明确描述。",
            "REQUIREMENT_NOT_CONFIRMED": "需求尚未确认。",
            "REQUIREMENT_OWNER_MISSING": "需求缺少负责人。",
            "REQUIREMENT_ACCEPTANCE_CRITERIA_MISSING": "需求缺少可测试的验收条件。",
            "RISK_TRIGGER_MISSING": "风险缺少触发信号。",
            "RISK_MITIGATION_MISSING": "风险缺少缓解动作。",
            "RISK_OWNER_MISSING": "风险缺少负责人。",
            "EVIDENCE_RELATION_MISSING": "该项目没有冻结证据关系。",
            "EVIDENCE_UNREVIEWED": "该项目仍有未复核证据。",
            "EVIDENCE_DISPUTED": "该项目包含有争议的证据。",
            "BLOCKING_RISK_UNRESOLVED": "仍有未解决的阻断风险。",
            "BLOCKING_DISAGREEMENT_UNRESOLVED": "仍有未解决的阻断分歧。",
        }

        def flatten_gaps(rows: Any, prefix: str) -> list[dict[str, str]]:
            result: list[dict[str, str]] = []
            for row in rows if isinstance(rows, list) else []:
                if not isinstance(row, dict):
                    continue
                item_id = str(row.get("item_id") or "")[:80]
                item_key = str(row.get("item_key") or f"{prefix}:{item_id}")[:180]
                for code in row.get("codes") if isinstance(row.get("codes"), list) else []:
                    clean_code = str(code or "")
                    if clean_code:
                        result.append({
                            "code": clean_code,
                            "item_key": item_key,
                            "message": messages.get(clean_code, "项目结构仍需复核。"),
                        })
            return result

        structural_gaps = [
            *flatten_gaps(projection.get("requirement_gaps"), "requirements"),
            *flatten_gaps(projection.get("risk_gaps"), "risks"),
        ]
        evidence_gaps = flatten_gaps(projection.get("evidence_gaps"), "evidence")
        public_blockers: list[dict[str, str]] = []
        for row in projection.get("blockers") if isinstance(projection.get("blockers"), list) else []:
            if not isinstance(row, dict):
                continue
            code = str(row.get("code") or "")
            item_id = str(row.get("item_id") or "")[:80]
            prefix = "disagreements" if "DISAGREEMENT" in code else "risks"
            if code:
                public_blockers.append({
                    "code": code,
                    "item_key": f"{prefix}:{item_id}",
                    "message": messages.get(code, "存在未解决阻断项。"),
                })
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
        public_port = {
            "port_id": str(port_resolution.get("port_id") or ""),
            "port_version": str(port_resolution.get("port_version") or ""),
            "contract_sha256": str(
                port_resolution.get("port_contract_sha256") or ""
            ),
            "input_schema_version": str(
                port_resolution.get("input_schema_version") or ""
            ),
            "input_schema_sha256": str(
                port_resolution.get("input_schema_sha256") or ""
            ),
            "output_schema_version": str(
                port_resolution.get("output_schema_version") or ""
            ),
            "output_schema_sha256": str(
                port_resolution.get("output_schema_sha256") or ""
            ),
            "provider_call_budget": int(
                port_resolution.get("provider_call_budget") or 0
            ),
            "market_read_budget": int(
                port_resolution.get("market_read_budget") or 0
            ),
            "business_write_budget": int(
                port_resolution.get("business_write_budget") or 0
            ),
            "failure_policy": str(port_resolution.get("failure_policy") or ""),
        }
        return {
            "version": "project_readiness_projection_v1",
            "integrity_ok": True,
            "metrics_visible": True,
            "room_id": str(room_id),
            "artifact_id": str(artifact_id),
            "artifact_version": int(artifact_version),
            "artifact_snapshot_sha256": str(
                binding.get("artifact_snapshot_sha256") or ""
            ),
            "evidence_graph_sha256": str(
                binding.get("evidence_relations_sha256") or ""
            ),
            "plugin_registry_snapshot_sha256": str(
                binding.get("plugin_registry_snapshot_sha256") or ""
            ),
            "resolution": {
                "version": "project_readiness_resolution_v1",
                "contribution": deepcopy(contribution),
                "adapter": deepcopy(adapter_ref),
                "port": public_port,
            },
            "state": str(projection.get("state") or "gaps_present"),
            "structural_gaps": structural_gaps,
            "blockers": public_blockers,
            "evidence_gaps": evidence_gaps,
            "provider_calls_performed": 0,
            "market_reads_performed": 0,
            "business_writes_performed": 0,
            "execution_capability": "none",
            "live_trading_allowed": False,
            "can_autonomously_decide": False,
            "ranking_produced": False,
            "winner_claim": False,
            "approval_produced": False,
            "user_final_decision_required": True,
            "can_replace_user_decision": False,
            "arbitrary_code_loading_allowed": False,
        }


__all__ = [
    "PROJECT_READINESS_ACTION_ID",
    "PROJECT_READINESS_ADAPTER_ID",
    "PROJECT_READINESS_CONTRIBUTION_ID",
    "PROJECT_READINESS_PORT_ID",
    "PROJECT_READINESS_RESPONSE_VERSION",
    "ProjectReadinessError",
    "ProjectReadinessService",
]
