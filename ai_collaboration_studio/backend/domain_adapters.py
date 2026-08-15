from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Protocol

from .capability_packs import room_has_capability
from .football_research import verify_football_research_contract
from .stock_research import verify_stock_research_contract
from .market.manual_official_evidence import apply_attested_earnings_overlay
from .market.storage_service import STORAGE_MARKET
from .plugin_registry import (
    DOMAIN_ADAPTER_CONTRACT_VERSION,
    DOMAIN_ADAPTER_CONTRACT_VERSION_V2,
    HOST_DOMAIN_ADAPTER_PORT_IDS,
    PluginRegistryError,
    plugin_registry_catalog,
    validate_room_plugin_registry_snapshot,
)


DOMAIN_ADAPTER_ID_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}")
OBSERVATION_PROPOSALS_PATTERN = re.compile(
    r"<observation_proposals>\s*(.*?)\s*</observation_proposals>",
    re.IGNORECASE | re.DOTALL,
)

# Capabilities that cannot be meaningful without a concrete domain adapter.
# This map makes a missing registration fail closed instead of silently
# degrading a professional room into the generic collaboration path.
CAPABILITY_ADAPTER_REQUIREMENTS = {
    "research.football.match_context.readonly": "football_research",
    "research.football.evidence_classification": "football_research",
    "research.stock.readonly_context": "stock_research",
    "research.stock.evidence_classification": "stock_research",
    "market.storage.readonly": "storage_research",
    "analytics.storage": "storage_research",
    "simulation.observations": "storage_research",
    "simulation.paper_portfolio": "storage_research",
    "decision.observation_proposals": "storage_research",
}


class DomainAdapterError(ValueError):
    """Base error for invalid or unavailable domain adapter configuration."""


class UnknownDomainAdapterError(DomainAdapterError):
    """Raised when a room requests an adapter that is not registered."""


@dataclass(frozen=True, slots=True)
class DomainMarketPreflight:
    gate: dict[str, Any]
    snapshot: dict[str, Any] | None
    capture_error: dict[str, str] | None = None


@dataclass(frozen=True, slots=True)
class DomainPersistenceResult:
    created_count: int = 0
    events: tuple[dict[str, Any], ...] = ()


class DomainCapabilityAdapter(Protocol):
    adapter_id: str
    adapter_version: str
    contract_version: str
    activation_capabilities: frozenset[str]
    execution_capability: str
    live_trading_allowed: bool
    provides_market_context: bool

    def with_market_service(self, market_service: Any) -> "DomainCapabilityAdapter": ...

    def preflight_market(
        self,
        room_snapshot: dict[str, Any],
        convergence: Any,
        *,
        prefetched_snapshot: dict[str, Any] | None = None,
        frozen: bool = False,
    ) -> DomainMarketPreflight: ...

    def prompt_context(self, snapshot: dict[str, Any] | None) -> str: ...

    def timeline_message(self, snapshot: dict[str, Any] | None) -> dict[str, str] | None: ...

    def speaker_prompt_rule(
        self,
        room: dict[str, Any],
        member: dict[str, Any],
        workflow_policy: dict[str, Any],
        *,
        direct_mention: bool,
    ) -> str: ...

    def machine_block_names(
        self,
        room: dict[str, Any],
        member: dict[str, Any],
        workflow_policy: dict[str, Any],
        *,
        direct_mention: bool,
    ) -> tuple[str, ...]: ...

    def extract_speaker_payloads(
        self,
        room: dict[str, Any],
        member: dict[str, Any],
        workflow_policy: dict[str, Any],
        content: str,
    ) -> tuple[str, list[dict[str, Any]]]: ...

    def persist_speaker_payloads(
        self,
        *,
        store: Any,
        room_id: str,
        round_id: str,
        member: dict[str, Any],
        public_member: dict[str, Any],
        message: dict[str, Any],
        payloads: list[dict[str, Any]],
        evidence_manifest: dict[str, Any] | None,
        market_snapshot: dict[str, Any] | None,
    ) -> DomainPersistenceResult: ...

    def artifact_prompt_rule(
        self,
        room: dict[str, Any],
        allowed_market_snapshot: dict[str, Any] | None,
    ) -> str: ...

    def artifact_evidence_types(self, room: dict[str, Any]) -> tuple[str, ...]: ...


@dataclass(frozen=True, slots=True)
class StorageResearchDomainAdapter:
    """Storage-industry capability implementation outside the room kernel."""

    market_service: Any = STORAGE_MARKET
    adapter_id: str = field(default="storage_research", init=False)
    adapter_version: str = field(default="1.0.0", init=False)
    contract_version: str = field(
        default=DOMAIN_ADAPTER_CONTRACT_VERSION,
        init=False,
    )
    activation_capabilities: frozenset[str] = field(
        default_factory=lambda: frozenset({
            "market.storage.readonly",
            "analytics.storage",
            "simulation.observations",
            "simulation.paper_portfolio",
            "decision.observation_proposals",
        }),
        init=False,
    )
    execution_capability: str = field(default="none", init=False)
    live_trading_allowed: bool = field(default=False, init=False)
    provides_market_context: bool = field(default=True, init=False)

    def with_market_service(self, market_service: Any) -> "StorageResearchDomainAdapter":
        return replace(self, market_service=market_service)

    def preflight_market(
        self,
        room_snapshot: dict[str, Any],
        convergence: Any,
        *,
        prefetched_snapshot: dict[str, Any] | None = None,
        frozen: bool = False,
    ) -> DomainMarketPreflight:
        room = room_snapshot.get("room") or {}
        if not room_has_capability(room, "market.storage.readonly"):
            raise DomainAdapterError("存储行情适配器不能处理未声明只读行情能力的房间。")

        snapshot = prefetched_snapshot if isinstance(prefetched_snapshot, dict) else None
        capture_error: dict[str, str] | None = None
        if not frozen and snapshot is None:
            if self.market_service is None:
                capture_error = {
                    "code": "MARKET_SERVICE_UNAVAILABLE",
                    "message": "只读行情服务未启用。",
                }
            else:
                try:
                    captured = self.market_service.snapshot()
                    snapshot = captured if isinstance(captured, dict) else None
                except Exception:
                    capture_error = {
                        "code": "MARKET_SERVICE_ERROR",
                        "message": "只读行情服务请求失败。",
                    }

        if not frozen and isinstance(snapshot, dict):
            snapshot = apply_attested_earnings_overlay(snapshot, room_snapshot)

        gate = convergence.market_preflight(room_snapshot, snapshot)
        return DomainMarketPreflight(
            gate=gate,
            snapshot=snapshot,
            capture_error=capture_error,
        )

    def prompt_context(self, snapshot: dict[str, Any] | None) -> str:
        if self.market_service is None or not isinstance(snapshot, dict):
            return ""
        return str(self.market_service.prompt_context(snapshot) or "")

    def timeline_message(self, snapshot: dict[str, Any] | None) -> dict[str, str] | None:
        if self.market_service is None or not isinstance(snapshot, dict):
            return None
        summary = str(self.market_service.timeline_summary(snapshot) or "").strip()
        if not summary:
            return None
        return {
            "sender_id": "futu_opend",
            "sender_name": "市场数据",
            "identity": "富途只读行情快照",
            "content": summary,
        }

    @staticmethod
    def _proposal_enabled(
        room: dict[str, Any],
        member: dict[str, Any],
        workflow_policy: dict[str, Any],
        *,
        direct_mention: bool,
    ) -> bool:
        stages = workflow_policy.get("stage_order") or []
        return bool(
            not direct_mention
            and room_has_capability(room, "decision.observation_proposals")
            and stages
            and str(member.get("workflow_stage") or "flexible") == str(stages[-1])
        )

    def speaker_prompt_rule(
        self,
        room: dict[str, Any],
        member: dict[str, Any],
        workflow_policy: dict[str, Any],
        *,
        direct_mention: bool,
    ) -> str:
        if not self._proposal_enabled(
            room,
            member,
            workflow_policy,
            direct_mention=direct_mention,
        ):
            return ""
        return (
            "你只能给出“候选最优研究方案”并送交用户复核，不能宣称系统已经替用户作出最终决定。"
            "若统一行情、反方、风控或证据仍有缺口，必须明确写成保留或退回，不能用模型信心填补。"
            "如果证据足以形成可验证观察，在正常中文结论之后追加且只追加一个机器可读块："
            '<observation_proposals>{"observations":[{"symbol":"US.MU|US.SNDK|US.WDC|US.STX",'
            '"direction":"UP|DOWN|NEUTRAL","horizon_days":1|5|20,"threshold_pct":数字,'
            '"thesis":"可验证依据","counter_case":"主要反证","model_confidence":0到100或null,'
            '"methodology_id":"稳定的方法标识","methodology_version":正整数,'
            '"evidence":{"material_ids":["真实资料ID"]}}]}</observation_proposals>。'
            "最多四条；没有充分证据则 observations 为空数组。资料版本和行情快照由系统按本轮冻结证据绑定，"
            "不要自行填写版本或快照。该块不会直接显示，也不会自动确认、下单或抓取基准价。"
        )

    def machine_block_names(
        self,
        room: dict[str, Any],
        member: dict[str, Any],
        workflow_policy: dict[str, Any],
        *,
        direct_mention: bool,
    ) -> tuple[str, ...]:
        return (
            ("observation_proposals",)
            if self._proposal_enabled(
                room,
                member,
                workflow_policy,
                direct_mention=direct_mention,
            )
            else ()
        )

    def extract_speaker_payloads(
        self,
        room: dict[str, Any],
        member: dict[str, Any],
        workflow_policy: dict[str, Any],
        content: str,
    ) -> tuple[str, list[dict[str, Any]]]:
        if not self._proposal_enabled(
            room,
            member,
            workflow_policy,
            direct_mention=False,
        ):
            return str(content or ""), []
        raw = str(content or "")
        proposals: list[dict[str, Any]] = []
        for match in OBSERVATION_PROPOSALS_PATTERN.finditer(raw):
            try:
                payload = json.loads(match.group(1))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            rows = payload.get("observations") if isinstance(payload, dict) else None
            if not isinstance(rows, list):
                continue
            proposals.extend(row for row in rows if isinstance(row, dict))
            if len(proposals) >= 4:
                break
        visible = OBSERVATION_PROPOSALS_PATTERN.sub("", raw).strip()
        return visible or "本轮没有补充可展示的文字结论。", proposals[:4]

    def persist_speaker_payloads(
        self,
        *,
        store: Any,
        room_id: str,
        round_id: str,
        member: dict[str, Any],
        public_member: dict[str, Any],
        message: dict[str, Any],
        payloads: list[dict[str, Any]],
        evidence_manifest: dict[str, Any] | None,
        market_snapshot: dict[str, Any] | None,
    ) -> DomainPersistenceResult:
        events: list[dict[str, Any]] = []
        created_count = 0
        for proposal_payload in payloads:
            evidence = (
                proposal_payload.get("evidence")
                if isinstance(proposal_payload.get("evidence"), dict)
                else {}
            )
            material_ids = (
                evidence.get("material_ids")
                if isinstance(evidence.get("material_ids"), list)
                else []
            )
            cited_by_id = {
                str(citation.get("id") or ""): citation
                for citation in message.get("citations") or []
                if str(citation.get("id") or "")
            }
            requested_material_ids = list(dict.fromkeys(
                str(material_id)
                for material_id in material_ids
                if str(material_id) in cited_by_id
            ))
            manifest_market_ref = (
                evidence_manifest.get("market_snapshot")
                if isinstance(evidence_manifest, dict)
                and isinstance(evidence_manifest.get("market_snapshot"), dict)
                else {}
            )
            try:
                observation = store.create_observation(room_id, {
                    **proposal_payload,
                    "created_by": member["id"],
                    "round_id": round_id,
                    "evidence": {
                        "material_ids": requested_material_ids,
                        "material_refs": [
                            {
                                "id": material_id,
                                "version": int(cited_by_id[material_id].get("version") or 0),
                            }
                            for material_id in requested_material_ids
                        ],
                        "message_ids": [message["id"]],
                        "market_snapshot_id": str(
                            manifest_market_ref.get("snapshot_id")
                            or (market_snapshot or {}).get("snapshot_id")
                            or ""
                        ),
                        "market_evidence_version": str(
                            manifest_market_ref.get("evidence_version") or ""
                        ),
                        "market_snapshot_sha256": str(
                            manifest_market_ref.get("snapshot_sha256") or ""
                        ),
                    },
                })
            except ValueError as exc:
                events.append({
                    "type": "observation_proposal_rejected",
                    "member": public_member,
                    "error": str(exc),
                })
                continue
            if observation:
                created_count += 1
                events.append({
                    "type": "observation_proposed",
                    "member": public_member,
                    "observation": observation,
                })
        return DomainPersistenceResult(
            created_count=created_count,
            events=tuple(events),
        )

    def artifact_prompt_rule(
        self,
        room: dict[str, Any],
        allowed_market_snapshot: dict[str, Any] | None,
    ) -> str:
        if not self.activation_capabilities.intersection(room.get("capabilities") or []):
            return ""
        market_rule = (
            "本轮还提供了唯一的冻结市场快照。只有确实用于某项主张时才可引用："
            f'{{"type":"round_market_snapshot","id":"{allowed_market_snapshot.get("id")}"}}。'
            "它只是冻结证据容器，不代表行情事实已由用户核验。"
            if allowed_market_snapshot and allowed_market_snapshot.get("id")
            else "本次没有可引用的轮次冻结市场快照，不得输出round_market_snapshot证据。"
        )
        return (
            f"{market_rule}"
            "存储产业研究只能形成研究、回测或模拟观察事项，不得生成真实下单动作。"
        )

    def artifact_evidence_types(self, room: dict[str, Any]) -> tuple[str, ...]:
        return (
            ("round_market_snapshot",)
            if self.activation_capabilities.intersection(room.get("capabilities") or [])
            else ()
        )


@dataclass(frozen=True, slots=True)
class FootballResearchDomainAdapter:
    """Pure validation port for one already material-bound football seal."""

    adapter_id: str = field(default="football_research", init=False)
    adapter_version: str = field(default="1.0.0", init=False)
    contract_version: str = field(
        default=DOMAIN_ADAPTER_CONTRACT_VERSION_V2,
        init=False,
    )
    activation_capabilities: frozenset[str] = field(
        default_factory=lambda: frozenset({
            "research.football.match_context.readonly",
            "research.football.evidence_classification",
        }),
        init=False,
    )
    declared_ports: frozenset[str] = field(
        default_factory=lambda: frozenset({"core.football.match_context/v1"}),
        init=False,
    )
    execution_capability: str = field(default="none", init=False)
    live_trading_allowed: bool = field(default=False, init=False)
    provides_market_context: bool = field(default=False, init=False)

    def project_football_match_context(
        self,
        *,
        contract: dict[str, Any],
    ) -> dict[str, Any]:
        """Return only a verified, closed v1 contract; perform no I/O."""

        try:
            return verify_football_research_contract(contract)
        except (TypeError, ValueError) as exc:
            raise DomainAdapterError(
                "football research contract failed exact verification"
            ) from exc


@dataclass(frozen=True, slots=True)
class StockResearchDomainAdapter:
    """Pure validation port for a room-scoped stock research seal."""

    adapter_id: str = field(default="stock_research", init=False)
    adapter_version: str = field(default="1.0.0", init=False)
    contract_version: str = field(
        default=DOMAIN_ADAPTER_CONTRACT_VERSION_V2,
        init=False,
    )
    activation_capabilities: frozenset[str] = field(
        default_factory=lambda: frozenset({
            "research.stock.readonly_context",
            "research.stock.evidence_classification",
        }),
        init=False,
    )
    declared_ports: frozenset[str] = field(
        default_factory=lambda: frozenset({"core.market.readonly_context/v1"}),
        init=False,
    )
    execution_capability: str = field(default="none", init=False)
    live_trading_allowed: bool = field(default=False, init=False)
    provides_market_context: bool = field(default=False, init=False)

    def project_market_readonly_context(
        self,
        *,
        contract: dict[str, Any],
    ) -> dict[str, Any]:
        """Return only an exact verified contract; perform no reads or writes."""

        try:
            return verify_stock_research_contract(contract)
        except (TypeError, ValueError) as exc:
            raise DomainAdapterError(
                "stock research contract failed exact verification"
            ) from exc


@dataclass(frozen=True, slots=True)
class ProjectReadinessDomainAdapter:
    """Pure deterministic projection; it never receives Store or service handles."""

    adapter_id: str = field(default="project_readiness", init=False)
    adapter_version: str = field(default="1.0.0", init=False)
    contract_version: str = field(
        default=DOMAIN_ADAPTER_CONTRACT_VERSION_V2,
        init=False,
    )
    activation_capabilities: frozenset[str] = field(
        default_factory=lambda: frozenset({"research.project.readiness_review"}),
        init=False,
    )
    declared_ports: frozenset[str] = field(
        default_factory=lambda: frozenset({"core.artifact.projection/v1"}),
        init=False,
    )
    execution_capability: str = field(default="none", init=False)
    live_trading_allowed: bool = field(default=False, init=False)

    @staticmethod
    def _items(value: Any) -> list[dict[str, Any]]:
        return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []

    def project_artifact(
        self,
        *,
        artifact: dict[str, Any],
        evidence_relations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        content = artifact.get("content") if isinstance(artifact.get("content"), dict) else {}
        relations_by_item: dict[str, list[dict[str, Any]]] = {}
        for relation in evidence_relations if isinstance(evidence_relations, list) else []:
            if not isinstance(relation, dict):
                continue
            item_key = str(relation.get("item_key") or "")[:180]
            if item_key:
                relations_by_item.setdefault(item_key, []).append(relation)

        requirement_gaps: list[dict[str, Any]] = []
        evidence_gaps: list[dict[str, Any]] = []
        risk_gaps: list[dict[str, Any]] = []
        blockers: list[dict[str, Any]] = []

        def append_evidence_gap(item_key: str, item_id: str) -> None:
            relations = relations_by_item.get(item_key, [])
            codes: list[str] = []
            if not relations:
                codes.append("EVIDENCE_RELATION_MISSING")
            elif any(
                str(item.get("verification_status") or "unreviewed") == "unreviewed"
                for item in relations
            ):
                codes.append("EVIDENCE_UNREVIEWED")
            if any(
                str(item.get("verification_status") or "") == "disputed"
                for item in relations
            ):
                codes.append("EVIDENCE_DISPUTED")
            if codes:
                evidence_gaps.append({
                    "item_key": item_key,
                    "item_id": item_id,
                    "codes": codes,
                })

        for index, requirement in enumerate(self._items(content.get("requirements"))):
            item_id = str(requirement.get("id") or f"requirement_{index + 1}")[:80]
            codes: list[str] = []
            if not str(requirement.get("text") or "").strip():
                codes.append("REQUIREMENT_TEXT_MISSING")
            if str(requirement.get("status") or "pending").lower() != "confirmed":
                codes.append("REQUIREMENT_NOT_CONFIRMED")
            if not str(requirement.get("owner") or "").strip():
                codes.append("REQUIREMENT_OWNER_MISSING")
            if not str(requirement.get("acceptance_criteria") or "").strip():
                codes.append("REQUIREMENT_ACCEPTANCE_CRITERIA_MISSING")
            if codes:
                requirement_gaps.append({"item_id": item_id, "codes": codes})
            append_evidence_gap(f"requirements:{item_id}", item_id)

        for index, risk in enumerate(self._items(content.get("risks"))):
            item_id = str(risk.get("id") or f"risk_{index + 1}")[:80]
            codes: list[str] = []
            for field_name, code in (
                ("trigger", "RISK_TRIGGER_MISSING"),
                ("mitigation", "RISK_MITIGATION_MISSING"),
                ("owner", "RISK_OWNER_MISSING"),
            ):
                if not str(risk.get(field_name) or "").strip():
                    codes.append(code)
            if codes:
                risk_gaps.append({"item_id": item_id, "codes": codes})
            if risk.get("blocking") is True and str(
                risk.get("status") or "open"
            ).lower() not in {"resolved", "closed", "accepted"}:
                blockers.append({
                    "item_id": item_id,
                    "code": "BLOCKING_RISK_UNRESOLVED",
                })
            append_evidence_gap(f"risks:{item_id}", item_id)

        for index, disagreement in enumerate(
            self._items(content.get("disagreements"))
        ):
            item_id = str(
                disagreement.get("id") or f"disagreement_{index + 1}"
            )[:80]
            if disagreement.get("blocking") is True and str(
                disagreement.get("status") or "open"
            ).lower() not in {"resolved", "closed"}:
                blockers.append({
                    "item_id": item_id,
                    "code": "BLOCKING_DISAGREEMENT_UNRESOLVED",
                })
            append_evidence_gap(f"disagreements:{item_id}", item_id)

        state = (
            "blocked"
            if blockers
            else "gaps_present"
            if requirement_gaps or evidence_gaps or risk_gaps
            else "ready"
        )
        return {
            "version": "project_readiness_projection_v1",
            "state": state,
            "requirement_gaps": requirement_gaps,
            "evidence_gaps": evidence_gaps,
            "risk_gaps": risk_gaps,
            "blockers": blockers,
            "counts": {
                "requirement_gap_count": len(requirement_gaps),
                "evidence_gap_count": len(evidence_gaps),
                "risk_gap_count": len(risk_gaps),
                "blocker_count": len(blockers),
            },
            "provider_calls_performed": 0,
            "market_reads_performed": 0,
            "business_writes_performed": 0,
            "ranking_produced": False,
            "winner_claim": False,
            "approval_produced": False,
            "user_final_decision_required": True,
            "can_replace_user_decision": False,
            "arbitrary_code_loading_allowed": False,
        }


@dataclass(frozen=True, slots=True)
class ProjectRoundFocusDomainAdapter:
    """Pure next-round focus projection over host-provided sealed data."""

    adapter_id: str = field(default="project_round_focus", init=False)
    adapter_version: str = field(default="1.0.0", init=False)
    contract_version: str = field(
        default=DOMAIN_ADAPTER_CONTRACT_VERSION_V2,
        init=False,
    )
    activation_capabilities: frozenset[str] = field(
        default_factory=lambda: frozenset({"research.project.round_focus"}),
        init=False,
    )
    declared_ports: frozenset[str] = field(
        default_factory=lambda: frozenset({"core.round.context/v1"}),
        init=False,
    )
    execution_capability: str = field(default="none", init=False)
    live_trading_allowed: bool = field(default=False, init=False)
    provides_market_context: bool = field(default=False, init=False)

    @staticmethod
    def _focus_rows(
        projection: dict[str, Any],
    ) -> list[dict[str, Any]]:
        groups = (
            ("blocker", projection.get("blockers"), ["critical_review"]),
            ("evidence", projection.get("evidence_gaps"), ["evidence_review"]),
            (
                "structural",
                projection.get("structural_gaps"),
                ["evidence_review", "decision_synthesis"],
            ),
        )
        result: list[dict[str, Any]] = []
        for category, rows, target_capabilities in groups:
            if not isinstance(rows, list):
                raise DomainAdapterError("project readiness gap rows are invalid")
            for row in rows:
                if not isinstance(row, dict) or set(row) != {
                    "code",
                    "item_key",
                    "message",
                }:
                    raise DomainAdapterError("a project readiness gap row is invalid")
                code = str(row.get("code") or "").strip()[:80]
                item_key = str(row.get("item_key") or "").strip()[:180]
                message = str(row.get("message") or "").strip()[:500]
                if not code or not item_key or not message:
                    raise DomainAdapterError("a project readiness gap row is incomplete")
                if len(result) < 16:
                    result.append({
                        "sequence_no": len(result) + 1,
                        "category": category,
                        "code": code,
                        "item_key": item_key,
                        "message": message,
                        "target_capabilities": list(target_capabilities),
                    })
        return result

    def project_round_context(
        self,
        *,
        artifact_binding: dict[str, Any],
        readiness_projection: dict[str, Any] | None,
        room_context: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(artifact_binding, dict) or not isinstance(room_context, dict):
            raise DomainAdapterError("project round focus input is invalid")
        status = str(artifact_binding.get("status") or "")
        objective = str(room_context.get("objective") or "").strip()[:4000]
        if status == "none":
            if readiness_projection not in (None, {}):
                raise DomainAdapterError("bootstrap focus cannot carry an artifact projection")
            state = "bootstrap"
            focus_items: list[dict[str, Any]] = []
            suggested_objective = objective
        elif status == "exact":
            if not isinstance(readiness_projection, dict):
                raise DomainAdapterError("artifact focus requires a readiness projection")
            state = str(readiness_projection.get("state") or "")
            if state not in {"ready", "gaps_present", "blocked"}:
                raise DomainAdapterError("readiness projection state is invalid")
            focus_items = self._focus_rows(readiness_projection)
            has_blocker = any(row["category"] == "blocker" for row in focus_items)
            if (
                (state == "ready" and focus_items)
                or (state == "gaps_present" and (not focus_items or has_blocker))
                or (state == "blocked" and not has_blocker)
            ):
                raise DomainAdapterError("readiness projection gaps are inconsistent")
            if focus_items:
                summary = "; ".join(
                    f"{row['code']} ({row['item_key']})" for row in focus_items[:8]
                )
                suggested_objective = (
                    "补齐已冻结的项目缺口：" + summary
                )[:4000]
            else:
                suggested_objective = objective
        else:
            raise DomainAdapterError("artifact binding status is invalid")
        counts = {
            "structural_gap_count": sum(
                1 for row in focus_items if row["category"] == "structural"
            ),
            "blocker_count": sum(
                1 for row in focus_items if row["category"] == "blocker"
            ),
            "evidence_gap_count": sum(
                1 for row in focus_items if row["category"] == "evidence"
            ),
            "focus_item_count": len(focus_items),
        }
        return {
            "version": "project_round_focus_projection_v1",
            "state": state,
            "counts": counts,
            "focus_items": focus_items,
            "suggested_objective": suggested_objective,
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


class DomainAdapterRegistry:
    """Validated registry for capability-driven domain extensions."""

    _required_methods = (
        "with_market_service",
        "preflight_market",
        "prompt_context",
        "timeline_message",
        "speaker_prompt_rule",
        "machine_block_names",
        "extract_speaker_payloads",
        "persist_speaker_payloads",
        "artifact_prompt_rule",
        "artifact_evidence_types",
    )

    def __init__(self, adapters: Iterable[Any] = ()) -> None:
        self._adapters: dict[str, Any] = {}
        self._adapters_by_exact: dict[tuple[str, str], Any] = {}
        for adapter in adapters:
            self.register(adapter)

    @classmethod
    def _validate(cls, adapter: Any) -> tuple[str, str]:
        adapter_id = str(getattr(adapter, "adapter_id", "") or "").strip().lower()
        if not DOMAIN_ADAPTER_ID_PATTERN.fullmatch(adapter_id):
            raise DomainAdapterError("领域适配器 ID 无效。")
        capabilities = getattr(adapter, "activation_capabilities", None)
        if not isinstance(capabilities, frozenset) or not capabilities:
            raise DomainAdapterError(f"领域适配器 {adapter_id} 必须声明非空能力集合。")
        if any(not str(capability or "").strip() for capability in capabilities):
            raise DomainAdapterError(f"领域适配器 {adapter_id} 包含无效能力。")
        if str(getattr(adapter, "execution_capability", "") or "").strip().lower() != "none":
            raise DomainAdapterError(f"领域适配器 {adapter_id} 违反不可执行边界。")
        if getattr(adapter, "live_trading_allowed", None) is not False:
            raise DomainAdapterError(f"领域适配器 {adapter_id} 违反禁止真实交易边界。")
        adapter_version = str(
            getattr(adapter, "adapter_version", "1.0.0") or "1.0.0"
        ).strip()
        if not re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", adapter_version):
            raise DomainAdapterError(f"领域适配器 {adapter_id} 版本无效。")
        contract_version = str(
            getattr(adapter, "contract_version", DOMAIN_ADAPTER_CONTRACT_VERSION)
            or DOMAIN_ADAPTER_CONTRACT_VERSION
        )
        if contract_version == DOMAIN_ADAPTER_CONTRACT_VERSION:
            for method_name in cls._required_methods:
                if not callable(getattr(adapter, method_name, None)):
                    raise DomainAdapterError(
                        f"领域适配器 {adapter_id} 缺少接口：{method_name}。"
                    )
        elif contract_version == DOMAIN_ADAPTER_CONTRACT_VERSION_V2:
            contracts = [
                row
                for row in plugin_registry_catalog().get("domain_adapters") or []
                if str(row.get("adapter_id") or "") == adapter_id
                and str(row.get("adapter_version") or "") == adapter_version
            ]
            if len(contracts) != 1:
                raise DomainAdapterError(
                    f"领域适配器 {adapter_id}@{adapter_version} 精确合同不可用。"
                )
            contract = contracts[0]
            declared_ports = getattr(adapter, "declared_ports", None)
            expected_ports = frozenset(
                str(row.get("port_id") or "")
                for row in contract.get("ports") or []
            )
            if (
                not isinstance(declared_ports, frozenset)
                or declared_ports != expected_ports
                or not declared_ports
                or not declared_ports.issubset(HOST_DOMAIN_ADAPTER_PORT_IDS)
            ):
                raise DomainAdapterError(
                    f"领域适配器 {adapter_id} 端口声明与精确合同不一致。"
                )
            for port in contract.get("ports") or []:
                handler = str(port.get("handler_method") or "")
                if not handler or not callable(getattr(adapter, handler, None)):
                    raise DomainAdapterError(
                        f"领域适配器 {adapter_id} 缺少端口实现：{handler or 'unknown'}。"
                    )
        else:
            raise DomainAdapterError(
                f"领域适配器 {adapter_id} 合同版本不受支持。"
            )
        return adapter_id, adapter_version

    def register(self, adapter: Any) -> None:
        adapter_id, adapter_version = self._validate(adapter)
        exact_key = (adapter_id, adapter_version)
        if exact_key in self._adapters_by_exact:
            raise DomainAdapterError(
                f"领域适配器重复注册：{adapter_id}@{adapter_version}。"
            )
        self._adapters_by_exact[exact_key] = adapter
        current = self._adapters.get(adapter_id)
        current_version = str(
            getattr(current, "adapter_version", "0.0.0") or "0.0.0"
        ) if current is not None else "0.0.0"
        if tuple(int(item) for item in adapter_version.split(".")) >= tuple(
            int(item) for item in current_version.split(".")
        ):
            self._adapters[adapter_id] = adapter

    def require(self, adapter_id: str, adapter_version: str | None = None) -> Any:
        clean_id = str(adapter_id or "").strip().lower()
        clean_version = str(adapter_version or "").strip()
        adapter = (
            self._adapters_by_exact.get((clean_id, clean_version))
            if clean_version
            else self._adapters.get(clean_id)
        )
        if adapter is None:
            identity = f"{clean_id}@{clean_version}" if clean_version else clean_id
            raise UnknownDomainAdapterError(f"未知领域适配器：{identity or 'empty'}。")
        self._validate(adapter)
        return adapter

    def has(self, adapter_id: str, adapter_version: str | None = None) -> bool:
        clean_id = str(adapter_id or "").strip().lower()
        clean_version = str(adapter_version or "").strip()
        return (
            (clean_id, clean_version) in self._adapters_by_exact
            if clean_version
            else clean_id in self._adapters
        )

    def active_for_room(self, room: dict[str, Any] | None) -> tuple[DomainCapabilityAdapter, ...]:
        room_data = room if isinstance(room, dict) else {}
        capabilities = {
            str(capability or "").strip()
            for capability in room_data.get("capabilities") or []
            if str(capability or "").strip()
        }
        frozen_registry = room_data.get("plugin_registry_snapshot")
        frozen_versions: dict[str, str] = {}
        frozen_contract_versions: dict[str, str] = {}
        lifecycle_targets: dict[tuple[str, str], dict[str, Any]] = {}
        if frozen_registry is not None:
            try:
                frozen_registry = validate_room_plugin_registry_snapshot(
                    frozen_registry,
                    room_data.get("capability_pack_ids") or [],
                    require_current=True,
                )
            except PluginRegistryError as exc:
                raise DomainAdapterError(
                    "房间冻结的插件 registry snapshot 无效。"
                ) from exc
            lifecycle_current = room_data.get("plugin_lifecycle_current")
            if (
                not isinstance(lifecycle_current, dict)
                or lifecycle_current.get("integrity_ok") is not True
            ):
                raise DomainAdapterError(
                    "room plugin lifecycle is unsealed or damaged"
                )
            raw_lifecycle_targets = lifecycle_current.get("targets")
            if not isinstance(raw_lifecycle_targets, list):
                raise DomainAdapterError(
                    "room plugin lifecycle target projection is invalid"
                )
            lifecycle_targets = {
                (
                    str(item.get("id") or ""),
                    str(item.get("version") or ""),
                ): item
                for item in raw_lifecycle_targets
                if isinstance(item, dict)
                and str(item.get("kind") or "") == "domain_adapter"
            }
            active_pack_ids = {
                str(item or "")
                for item in (
                    room_data.get("active_capability_pack_ids")
                    if isinstance(
                        room_data.get("active_capability_pack_ids"),
                        list,
                    )
                    else room_data.get("capability_pack_ids") or []
                )
                if str(item or "")
            }
            raw_declared = [
                str(item.get("adapter_id") or "")
                for item in frozen_registry.get("domain_adapters") or []
                if isinstance(item, dict)
                and (
                    not item.get("pack_ids")
                    or any(
                        str(pack_id or "") in active_pack_ids
                        for pack_id in item.get("pack_ids") or []
                    )
                )
            ]
            frozen_versions = {
                str(item.get("adapter_id") or ""): str(
                    item.get("adapter_version") or ""
                )
                for item in frozen_registry.get("domain_adapters") or []
                if isinstance(item, dict)
            }
            frozen_contract_versions = {
                str(item.get("adapter_id") or ""): str(
                    item.get("contract_version")
                    or DOMAIN_ADAPTER_CONTRACT_VERSION
                )
                for item in frozen_registry.get("domain_adapters") or []
                if isinstance(item, dict)
            }
        else:
            raw_declared = room_data.get("domain_adapter_ids", [])
        if not isinstance(raw_declared, list):
            raise DomainAdapterError("domain_adapter_ids 必须是字符串数组。")
        required_ids = [
            str(adapter_id or "").strip().lower()
            for adapter_id in raw_declared
            if str(adapter_id or "").strip()
        ]
        for capability in sorted(capabilities):
            required_id = CAPABILITY_ADAPTER_REQUIREMENTS.get(capability)
            if required_id and required_id not in required_ids:
                if frozen_registry is not None:
                    raise DomainAdapterError(
                        f"房间冻结 registry 缺少能力 {capability} 所需适配器。"
                    )
                required_ids.append(required_id)
        if frozen_registry is None:
            for adapter_id, adapter in self._adapters.items():
                if adapter.activation_capabilities.intersection(capabilities) and adapter_id not in required_ids:
                    required_ids.append(adapter_id)
        active = tuple(
            self.require(
                adapter_id,
                frozen_versions.get(adapter_id) if frozen_registry is not None else None,
            )
            for adapter_id in required_ids
        )
        if frozen_registry is not None:
            for adapter in active:
                adapter_id = str(getattr(adapter, "adapter_id", "") or "")
                adapter_version = str(
                    getattr(adapter, "adapter_version", "") or ""
                )
                lifecycle_target = lifecycle_targets.get(
                    (adapter_id, adapter_version)
                )
                if (
                    str(getattr(adapter, "contract_version", "") or "")
                    != frozen_contract_versions.get(adapter_id)
                    or adapter_version != frozen_versions.get(adapter_id)
                    or not lifecycle_target
                    or lifecycle_target.get("integrity_ok") is not True
                    or lifecycle_target.get("runtime_available") is not True
                ):
                    raise DomainAdapterError(
                        f"房间冻结的领域适配器实现版本不可用：{adapter_id}。"
                    )
        return active

    def market_adapter_for(
        self,
        room: dict[str, Any] | None,
    ) -> DomainCapabilityAdapter | None:
        capabilities = {
            str(capability or "").strip()
            for capability in (room or {}).get("capabilities") or []
            if str(capability or "").strip()
        }
        candidates = [
            adapter
            for adapter in self.active_for_room(room)
            if getattr(adapter, "provides_market_context", False)
            and adapter.activation_capabilities.intersection(capabilities)
        ]
        if len(candidates) > 1:
            raise DomainAdapterError("房间同时激活了多个行情领域适配器。")
        return candidates[0] if candidates else None

    def with_market_service(
        self,
        adapter_id: str,
        market_service: Any,
    ) -> "DomainAdapterRegistry":
        target = self.require(adapter_id)
        replacement = target.with_market_service(market_service)
        target_version = str(getattr(target, "adapter_version", "1.0.0") or "1.0.0")
        adapters = [
            replacement
            if current_id == str(adapter_id).strip().lower()
            and current_version == target_version
            else adapter
            for (current_id, current_version), adapter in self._adapters_by_exact.items()
        ]
        return DomainAdapterRegistry(adapters)

    def require_port_resolution(self, resolution: dict[str, Any]) -> Any:
        if not isinstance(resolution, dict):
            raise DomainAdapterError("领域适配器端口解析无效。")
        adapter_id = str(resolution.get("adapter_id") or "")
        adapter_version = str(resolution.get("adapter_version") or "")
        port_id = str(resolution.get("port_id") or "")
        adapter = self.require(adapter_id, adapter_version)
        if port_id not in getattr(adapter, "declared_ports", frozenset()):
            raise DomainAdapterError("领域适配器未声明请求的端口。")
        contracts = [
            row
            for row in plugin_registry_catalog().get("domain_adapters") or []
            if str(row.get("adapter_id") or "") == adapter_id
            and str(row.get("adapter_version") or "") == adapter_version
        ]
        if len(contracts) != 1 or str(contracts[0].get("contract_sha256") or "") != str(
            resolution.get("adapter_contract_sha256") or ""
        ):
            raise DomainAdapterError("领域适配器精确合同哈希不可用。")
        port = next((
            item
            for item in contracts[0].get("ports") or []
            if str(item.get("port_id") or "") == port_id
            and str(item.get("port_version") or "")
            == str(resolution.get("port_version") or "")
            and str(item.get("contract_sha256") or "")
            == str(resolution.get("port_contract_sha256") or "")
        ), None)
        handler = str((port or {}).get("handler_method") or "")
        if not port or not handler or not callable(getattr(adapter, handler, None)):
            raise DomainAdapterError("领域适配器精确端口实现不可用。")
        return adapter

    def artifact_prompt_rules(
        self,
        room: dict[str, Any] | None,
        allowed_market_snapshot: dict[str, Any] | None,
    ) -> str:
        return "".join(
            adapter.artifact_prompt_rule(room or {}, allowed_market_snapshot)
            for adapter in self.active_for_room(room)
            if callable(getattr(adapter, "artifact_prompt_rule", None))
        )

    def artifact_evidence_types(self, room: dict[str, Any] | None) -> tuple[str, ...]:
        values: list[str] = []
        for adapter in self.active_for_room(room):
            method = getattr(adapter, "artifact_evidence_types", None)
            if callable(method):
                values.extend(method(room or {}))
        return tuple(dict.fromkeys(value for value in values if value))


DEFAULT_STORAGE_MARKET_SERVICE = STORAGE_MARKET
DEFAULT_DOMAIN_ADAPTERS = DomainAdapterRegistry((
    FootballResearchDomainAdapter(),
    StockResearchDomainAdapter(),
    StorageResearchDomainAdapter(),
    ProjectReadinessDomainAdapter(),
    ProjectRoundFocusDomainAdapter(),
))
