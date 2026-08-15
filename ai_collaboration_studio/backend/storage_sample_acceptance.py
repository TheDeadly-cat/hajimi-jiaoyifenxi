from __future__ import annotations

import re
from typing import Any

from .capability_packs import room_has_capability
from .convergence import ConvergenceService
from .store import (
    OBSERVATION_MIN_SAMPLES,
    OBSERVATION_SCORECARD_VERSION,
    StudioStore,
)
from .templates import STORAGE_RESEARCH_CAPABILITY_PACKS
from .turn_contract import TURN_CONTRACT_VERSION
from .user_decision import (
    USER_DECISION_ACTIONS,
    USER_DECISION_VERSION,
    preferred_option_id,
)
from .workflow_policy import member_matches_requirement, policy_from_json


STORAGE_SAMPLE_ACCEPTANCE_LEGACY_VERSION = "storage_sample_acceptance_v1"
STORAGE_SAMPLE_ACCEPTANCE_PREVIOUS_VERSION = "storage_sample_acceptance_v2"
STORAGE_SAMPLE_ACCEPTANCE_VERSION = "storage_sample_acceptance_v3"
STORAGE_TEMPLATE_ID = "us_storage_committee"
STORAGE_CHECKPOINT_VERSION = 7
STORAGE_QUALIFIED_ROLE_COUNT = 12


class StorageSampleAcceptance:
    """Read-only, deterministic acceptance audit for the storage sample room.

    The audit consumes only persisted room state. It never asks a provider for a
    completion, refreshes market data, migrates a checkpoint, or writes a result
    back to SQLite. Statistical validation is intentionally reported separately
    from the operational acceptance result.
    """

    def __init__(
        self,
        store: StudioStore,
        convergence: ConvergenceService | None = None,
    ) -> None:
        self.store = store
        self.convergence = convergence or ConvergenceService(store)

    def evaluate(
        self,
        room_id: str,
        *,
        snapshot: dict[str, Any] | None = None,
        convergence_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        room_snapshot = (
            snapshot
            if isinstance(snapshot, dict)
            else self.store.room_snapshot(room_id)
        )
        if not room_snapshot:
            return self._missing_room_result(room_id)

        room = (
            room_snapshot.get("room")
            if isinstance(room_snapshot.get("room"), dict)
            else {}
        )
        scorecard = (
            room_snapshot.get("observation_scorecard")
            if isinstance(room_snapshot.get("observation_scorecard"), dict)
            else {}
        )
        statistical_validation = self._statistical_validation(scorecard)
        has_lifecycle_projection = isinstance(
            room.get("active_capability_pack_ids"),
            list,
        )
        room_pack_ids = {
            str(item)
            for item in (
                room.get("active_capability_pack_ids")
                if has_lifecycle_projection
                else room.get("capability_pack_ids") or []
            )
            if str(item)
        }
        if not (
            room_has_capability(room, "market.storage.readonly")
            or "storage_research_readonly" in room_pack_ids
        ):
            return self._not_applicable_result(
                room_id,
                room,
                statistical_validation,
            )

        latest_round = (
            room_snapshot.get("latest_round")
            if isinstance(room_snapshot.get("latest_round"), dict)
            else None
        )
        round_id = str((latest_round or {}).get("id") or "")
        checkpoint = (
            self.store.get_round_checkpoint(room_id, round_id)
            if round_id
            else None
        )
        checkpoint_state = (
            checkpoint.get("state")
            if isinstance(checkpoint, dict)
            and isinstance(checkpoint.get("state"), dict)
            else {}
        )
        contract_bundle = (
            self.store.round_turn_contract_bundle(room_id, round_id)
            if round_id
            else {
                "applicable": False,
                "valid": False,
                "messages": [],
                "issues": [],
            }
        )
        artifacts = self.store.list_artifacts(room_id) if round_id else []
        round_artifacts = [
            artifact
            for artifact in artifacts
            if str(artifact.get("round_id") or "") == round_id
        ]

        evaluated_convergence: dict[str, Any] = (
            convergence_state
            if isinstance(convergence_state, dict)
            else {}
        )
        convergence_error = ""
        if round_id and not evaluated_convergence:
            try:
                evaluated = self.convergence.evaluate(
                    room_id,
                    round_id=round_id,
                    snapshot=room_snapshot,
                )
                if isinstance(evaluated, dict):
                    evaluated_convergence = evaluated
            except (LookupError, TypeError, ValueError) as exc:
                convergence_error = str(exc)[:500]

        checks: dict[str, dict[str, Any]] = {}

        def add_check(
            check_id: str,
            *,
            ready: bool,
            code: str,
            expected: Any,
            actual: Any,
            detail: str,
        ) -> None:
            checks[check_id] = {
                "ready": ready is True,
                "required": True,
                "code": code,
                "expected": expected,
                "actual": actual,
                "detail": detail,
            }

        add_check(
            "latest_round",
            ready=bool(round_id),
            code="LATEST_ROUND_REQUIRED",
            expected="persisted latest round",
            actual=round_id or None,
            detail="尚无可验收轮次；验收器不会自动创建或替换轮次。",
        )

        round_status = str((latest_round or {}).get("status") or "").upper()
        add_check(
            "round_completed",
            ready=round_status == "COMPLETED",
            code="LATEST_ROUND_NOT_COMPLETED",
            expected="COMPLETED",
            actual=round_status or None,
            detail="只有已持久化且状态为已完成的最新轮次可以通过验收。",
        )

        checkpoint_version = self._safe_int(checkpoint_state.get("version"))
        add_check(
            "checkpoint_v7",
            ready=bool(checkpoint) and checkpoint_version >= STORAGE_CHECKPOINT_VERSION,
            code="CHECKPOINT_V7_REQUIRED",
            expected=STORAGE_CHECKPOINT_VERSION,
            actual=checkpoint_version if checkpoint else None,
            detail="缺失或早于 v7 的检查点不会被验收器升级或补写。",
        )

        frozen_pack_ids = [
            str(item)
            for item in checkpoint_state.get("capability_pack_ids") or []
            if str(item)
        ]
        expected_pack_ids = list(STORAGE_RESEARCH_CAPABILITY_PACKS)
        packs_ready = (
            len(frozen_pack_ids) == len(expected_pack_ids)
            and set(frozen_pack_ids) == set(expected_pack_ids)
        )
        add_check(
            "frozen_capability_packs",
            ready=packs_ready,
            code="CAPABILITY_PACKS_MISMATCH",
            expected=expected_pack_ids,
            actual=frozen_pack_ids,
            detail="最新轮次检查点必须冻结两个指定能力包。",
        )

        bundle_messages = [
            message
            for message in contract_bundle.get("messages") or []
            if isinstance(message, dict)
        ]
        qualified_messages = [
            message
            for message in bundle_messages
            if message.get("sender_type") == "ai"
            and message.get("turn_contract_version") == TURN_CONTRACT_VERSION
            and message.get("turn_contract_qualified") is True
            and message.get("turn_contract_integrity_ok") is True
            and message.get("member_snapshot_integrity_ok") is True
            and isinstance(message.get("member_snapshot"), dict)
        ]
        turn_contract_ready = bool(
            round_id
            and (latest_round or {}).get("turn_contract_version") == TURN_CONTRACT_VERSION
            and checkpoint_state.get("turn_contract_version") == TURN_CONTRACT_VERSION
            and checkpoint_state.get("turn_contract_required") is True
            and contract_bundle.get("applicable") is True
            and contract_bundle.get("valid") is True
            and contract_bundle.get("turn_contract_version") == TURN_CONTRACT_VERSION
            and qualified_messages
            and len(qualified_messages) == len(bundle_messages)
        )
        add_check(
            "turn_contract_v1",
            ready=turn_contract_ready,
            code="TURN_CONTRACT_V1_REQUIRED",
            expected={
                "version": TURN_CONTRACT_VERSION,
                "applicable": True,
                "valid": True,
                "all_formal_turns_qualified": True,
            },
            actual={
                "round_version": (latest_round or {}).get("turn_contract_version"),
                "checkpoint_version": checkpoint_state.get("turn_contract_version"),
                "checkpoint_required": checkpoint_state.get("turn_contract_required") is True,
                "bundle_applicable": contract_bundle.get("applicable") is True,
                "bundle_valid": contract_bundle.get("valid") is True,
                "bundle_message_count": len(bundle_messages),
                "qualified_message_count": len(qualified_messages),
                "issue_count": len(contract_bundle.get("issues") or []),
            },
            detail="只有通过账本复核的 turn_contract_v1 正式发言才计入验收。",
        )

        workflow_policy = policy_from_json(
            checkpoint_state.get("workflow_policy"),
            STORAGE_TEMPLATE_ID,
        )
        role_audit = self._qualified_role_audit(
            qualified_messages,
            checkpoint_state,
            workflow_policy,
        )
        add_check(
            "twelve_unique_qualified_roles",
            ready=role_audit["ready"],
            code="QUALIFIED_ROLE_SET_INVALID",
            expected={
                "unique_members": STORAGE_QUALIFIED_ROLE_COUNT,
                "unique_role_slots": STORAGE_QUALIFIED_ROLE_COUNT,
                "one_member_per_role_slot": True,
            },
            actual={
                "qualified_turn_count": role_audit["qualified_turn_count"],
                "unique_member_count": role_audit["unique_member_count"],
                "role_slot_count": role_audit["role_slot_count"],
                "matched_role_slot_count": role_audit["matched_role_slot_count"],
                "checkpoint_success_count": role_audit["checkpoint_success_count"],
                "member_version_drift_ids": role_audit["member_version_drift_ids"],
            },
            detail="十二个必需职责槽位必须与十二名冻结、合格的发言成员一一对应。",
        )
        add_check(
            "independent_data_guardian",
            ready=role_audit["data_guardian_ready"],
            code="DATA_GUARDIAN_NOT_INDEPENDENT",
            expected={
                "role_slot_count": 1,
                "matching_member_count": 1,
                "assigned_distinct_member_count": 1,
            },
            actual={
                "role_slot_count": role_audit["data_guardian_role_slot_count"],
                "matching_member_ids": role_audit["data_guardian_candidate_ids"],
                "assigned_member_ids": role_audit["data_guardian_member_ids"],
            },
            detail="冻结的数据质量官必须是独立合格角色，不能由其他岗位重复冒充。",
        )

        convergence_gates = self._convergence_gate_summary(evaluated_convergence)
        for check_id, code in (
            ("data_gate", "CONVERGENCE_DATA_GATE_BLOCKED"),
            ("research_evidence_gate", "CONVERGENCE_RESEARCH_GATE_BLOCKED"),
            ("turn_contract_gate", "CONVERGENCE_CONTRACT_GATE_BLOCKED"),
            ("discussion_gate", "CONVERGENCE_DISCUSSION_GATE_BLOCKED"),
            ("evidence_gate", "CONVERGENCE_EVIDENCE_GATE_BLOCKED"),
            ("user_decision_gate", "CONVERGENCE_USER_DECISION_GATE_BLOCKED"),
        ):
            gate = convergence_gates[check_id]
            add_check(
                f"convergence_{check_id}",
                ready=gate["ready"],
                code=code,
                expected={"ready": True},
                actual=gate,
                detail="当前持久化轮次尚未通过对应的收敛门。",
            )

        sole_artifact = round_artifacts[0] if len(round_artifacts) == 1 else None
        add_check(
            "single_round_artifact",
            ready=len(round_artifacts) == 1,
            code="ROUND_ARTIFACT_COUNT_INVALID",
            expected=1,
            actual={
                "count": len(round_artifacts),
                "artifact_ids": [str(item.get("id") or "") for item in round_artifacts],
            },
            detail="本轮必须且只能有一份产物；验收器不会替重复产物做选择，也不会补造缺失产物。",
        )
        artifact_confirmed = bool(
            sole_artifact
            and str(sole_artifact.get("status") or "").upper() == "CONFIRMED"
        )
        add_check(
            "artifact_confirmed",
            ready=artifact_confirmed,
            code="ROUND_ARTIFACT_UNCONFIRMED",
            expected="CONFIRMED",
            actual=(
                str(sole_artifact.get("status") or "").upper()
                if sole_artifact
                else None
            ),
            detail="最新轮次的唯一产物必须已由用户确认。",
        )

        evidence_audit = self._evidence_audit(
            sole_artifact,
            evaluated_convergence.get("evidence_gate")
            if isinstance(evaluated_convergence.get("evidence_gate"), dict)
            else {},
        )
        add_check(
            "all_evidence_reviewed",
            ready=evidence_audit["ready"],
            code="EVIDENCE_REVIEW_INCOMPLETE",
            expected={
                "evidence_count_minimum": 1,
                "unreviewed_count": 0,
                "reviewed_equals_total": True,
                "confirmation_ready": True,
            },
            actual=evidence_audit,
            detail="每条证据关系都必须明确复核；没有证据不能视为自动通过。",
        )

        decision_gate = (
            evaluated_convergence.get("decision_gate")
            if isinstance(evaluated_convergence.get("decision_gate"), dict)
            else {}
        )
        add_check(
            "candidate_decision",
            ready=decision_gate.get("ready") is True,
            code="CONVERGENCE_DECISION_GATE_BLOCKED",
            expected={"ready": True},
            actual={
                "ready": decision_gate.get("ready") is True,
                "status": decision_gate.get("status"),
                "option_count": self._safe_int(decision_gate.get("option_count")),
                "preferred_option_id": str(decision_gate.get("preferred_option_id") or ""),
            },
            detail="已确认产物必须保留有效的多方案候选决策。",
        )

        decision_binding = self._user_decision_binding(
            sole_artifact,
            evaluated_convergence.get("user_decision_gate")
            if isinstance(evaluated_convergence.get("user_decision_gate"), dict)
            else {},
        )
        add_check(
            "exact_user_decision_binding",
            ready=decision_binding["ready"],
            code="USER_DECISION_BINDING_INVALID",
            expected={
                "current": True,
                "decision_version": USER_DECISION_VERSION,
                "artifact_id_and_version_exact": True,
                "artifact_snapshot_sha256_exact": True,
                "ai_preferred_option_exact": True,
                "selected_option_exact": True,
            },
            actual=decision_binding,
            detail="当前用户决定必须精确绑定唯一确认产物、AI 首选与用户所选方案。",
        )

        if convergence_error:
            add_check(
                "convergence_evaluation",
                ready=False,
                code="CONVERGENCE_EVALUATION_FAILED",
                expected="successful read-only evaluation",
                actual=convergence_error,
                detail="收敛检查执行失败，已按失败关闭处理，未修改任何持久化状态。",
            )

        legacy_reasons = self._legacy_reasons(
            latest_round,
            checkpoint,
            checkpoint_version,
            contract_bundle,
        )
        meeting_check_ids = tuple(checks)
        meeting_reviewed = bool(
            round_id
            and not legacy_reasons
            and all(checks[check_id]["ready"] is True for check_id in meeting_check_ids)
        )

        user_decision_action = str(decision_binding.get("action") or "")
        support_selected = user_decision_action == "support"
        support_code = (
            "USER_DECISION_DEFERRED"
            if user_decision_action == "hold"
            else "USER_DECISION_RETURNED"
            if user_decision_action == "return"
            else "USER_DECISION_SUPPORT_REQUIRED"
        )
        support_detail = (
            "用户已选择暂时保留；会议复核可以完成，但该决定不会进入模拟组合研究验收。"
            if user_decision_action == "hold"
            else "用户已退回当前方案；会议复核记录保留，但修订并重新确认前不能进入研究样板验收。"
            if user_decision_action == "return"
            else "研究样板只有在用户明确支持当前候选后，才能进入关联模拟组合风险复核。"
        )
        add_check(
            "supported_user_decision",
            ready=support_selected,
            code=support_code,
            expected="support",
            actual=user_decision_action or None,
            detail=support_detail,
        )

        paper_portfolio_gate = self._paper_portfolio_gate_audit(
            evaluated_convergence.get("portfolio_gate")
            if isinstance(evaluated_convergence.get("portfolio_gate"), dict)
            else {},
            decision_binding,
        )
        add_check(
            "paper_portfolio_gate",
            ready=paper_portfolio_gate["ready"],
            code="PAPER_PORTFOLIO_GATE_BLOCKED",
            expected={
                "user_decision_action": "support",
                "current_decision_package_integrity": True,
                "exact_confirmed_risk_ready_portfolio_count_minimum": 1,
                "execution_capability": "none",
                "live_trading_allowed": False,
            },
            actual=paper_portfolio_gate,
            detail=(
                "当前支持决定必须由完整决定包精确关联至少一份已确认、通过风险门且保持只读边界的纸面组合。"
            ),
        )

        convergence_research_ready = bool(
            support_selected
            and evaluated_convergence.get("research_ready") is True
            and paper_portfolio_gate["ready"]
        )
        add_check(
            "convergence_research_ready",
            ready=convergence_research_ready,
            code="CONVERGENCE_RESEARCH_NOT_READY",
            expected={"research_ready": True},
            actual={
                "research_ready": evaluated_convergence.get("research_ready") is True,
                "user_decision_action": user_decision_action or None,
                "paper_portfolio_gate_ready": paper_portfolio_gate["ready"],
            },
            detail="验收器复用收敛服务的 research_ready 结论，不用较弱的本地条件替代当前决定包与纸面组合风险门。",
        )

        failed_checks = [item for item in checks.values() if item["ready"] is not True]
        research_sample_ready = bool(
            meeting_reviewed
            and support_selected
            and paper_portfolio_gate["ready"]
            and convergence_research_ready
            and not failed_checks
        )
        if research_sample_ready:
            workflow_state = "accepted"
        elif meeting_reviewed and user_decision_action == "hold":
            workflow_state = "deferred"
        elif meeting_reviewed and user_decision_action == "return":
            workflow_state = "returned"
        else:
            workflow_state = "blocked"
        blockers = self._blockers(checks, legacy_reasons)

        result = {
            "schema_version": STORAGE_SAMPLE_ACCEPTANCE_VERSION,
            "room_id": room_id,
            "applicable": True,
            "state": workflow_state,
            "acceptance_ready": research_sample_ready,
            "meeting_reviewed": meeting_reviewed,
            "research_sample_ready": research_sample_ready,
            "user_decision_action": user_decision_action,
            "paper_portfolio_gate": paper_portfolio_gate,
            "blocked": not research_sample_ready,
            "legacy": bool(legacy_reasons),
            "latest_round": {
                "id": round_id or None,
                "status": round_status or None,
                "turn_contract_version": (latest_round or {}).get("turn_contract_version"),
            },
            "checks": checks,
            "blockers": blockers,
            "role_audit": role_audit,
            "convergence_gates": convergence_gates,
            "statistical_validation": statistical_validation,
            "statistical_validation_ready": statistical_validation["ready"],
            "compatibility": self._compatibility_contract(meeting_reviewed),
            "read_only": True,
            "provider_calls": 0,
            "market_calls": 0,
            "historical_backfill_performed": False,
            "execution_capability": "none",
            "live_trading_allowed": False,
            "can_autonomously_decide": False,
        }
        return self._with_frontend_contract(result)

    @classmethod
    def _qualified_role_audit(
        cls,
        messages: list[dict[str, Any]],
        checkpoint_state: dict[str, Any],
        workflow_policy: dict[str, Any],
    ) -> dict[str, Any]:
        snapshots_by_member: dict[str, dict[str, Any]] = {}
        member_order: list[str] = []
        versions_by_member: dict[str, set[int]] = {}
        for message in sorted(
            messages,
            key=lambda item: (
                cls._safe_int(item.get("turn_order")),
                str(item.get("id") or ""),
            ),
        ):
            member_id = str(message.get("sender_id") or "")
            snapshot = message.get("member_snapshot")
            if not member_id or not isinstance(snapshot, dict):
                continue
            versions_by_member.setdefault(member_id, set()).add(
                cls._safe_int(message.get("member_version"))
            )
            if member_id not in snapshots_by_member:
                snapshots_by_member[member_id] = snapshot
                member_order.append(member_id)

        role_slots: list[dict[str, Any]] = []
        requirements = [
            item
            for item in workflow_policy.get("required_coverage") or []
            if isinstance(item, dict)
        ]
        for requirement in requirements:
            minimum = max(0, cls._safe_int(requirement.get("minimum")))
            for ordinal in range(1, minimum + 1):
                role_slots.append({
                    "slot_id": f"{str(requirement.get('id') or '')}:{ordinal}",
                    "requirement_id": str(requirement.get("id") or ""),
                    "requirement": requirement,
                })

        member_to_slot: dict[str, int] = {}

        def assign(slot_index: int, visited: set[str]) -> bool:
            requirement = role_slots[slot_index]["requirement"]
            for member_id in member_order:
                if member_id in visited:
                    continue
                if not member_matches_requirement(
                    snapshots_by_member[member_id],
                    requirement,
                ):
                    continue
                visited.add(member_id)
                previous_slot = member_to_slot.get(member_id)
                if previous_slot is None or assign(previous_slot, visited):
                    member_to_slot[member_id] = slot_index
                    return True
            return False

        for slot_index in range(len(role_slots)):
            assign(slot_index, set())

        slot_to_member = {
            slot_index: member_id
            for member_id, slot_index in member_to_slot.items()
        }
        assignments = [
            {
                "slot_id": slot["slot_id"],
                "requirement_id": slot["requirement_id"],
                "member_id": slot_to_member.get(index, ""),
            }
            for index, slot in enumerate(role_slots)
        ]
        qualified_member_ids = set(snapshots_by_member)
        checkpoint_success_ids = {
            str(item)
            for item in checkpoint_state.get("successful_member_ids") or []
            if str(item)
        }
        member_version_drift_ids = sorted(
            member_id
            for member_id, versions in versions_by_member.items()
            if len(versions) != 1 or 0 in versions
        )
        role_ready = bool(
            len(qualified_member_ids) == STORAGE_QUALIFIED_ROLE_COUNT
            and len(role_slots) == STORAGE_QUALIFIED_ROLE_COUNT
            and len(member_to_slot) == STORAGE_QUALIFIED_ROLE_COUNT
            and checkpoint_success_ids == qualified_member_ids
            and not member_version_drift_ids
        )

        data_slots = [
            item for item in role_slots if item["requirement_id"] == "data_quality"
        ]
        data_requirement = (
            data_slots[0]["requirement"]
            if len(data_slots) == 1
            else None
        )
        data_candidates = [
            member_id
            for member_id in member_order
            if data_requirement
            and member_matches_requirement(
                snapshots_by_member[member_id],
                data_requirement,
            )
        ]
        data_assigned = [
            item["member_id"]
            for item in assignments
            if item["requirement_id"] == "data_quality" and item["member_id"]
        ]
        data_guardian_ready = bool(
            role_ready
            and len(data_slots) == 1
            and len(data_candidates) == 1
            and len(set(data_assigned)) == 1
            and data_assigned[0] == data_candidates[0]
        )

        return {
            "ready": role_ready,
            "qualified_turn_count": len(messages),
            "unique_member_count": len(qualified_member_ids),
            "checkpoint_success_count": len(checkpoint_success_ids),
            "role_slot_count": len(role_slots),
            "matched_role_slot_count": len(member_to_slot),
            "member_version_drift_ids": member_version_drift_ids,
            "assignments": assignments,
            "data_guardian_ready": data_guardian_ready,
            "data_guardian_role_slot_count": len(data_slots),
            "data_guardian_candidate_ids": sorted(data_candidates),
            "data_guardian_member_ids": sorted(set(data_assigned)),
        }

    @classmethod
    def _convergence_gate_summary(
        cls,
        convergence_state: dict[str, Any],
    ) -> dict[str, dict[str, Any]]:
        summary: dict[str, dict[str, Any]] = {}
        for gate_id in (
            "data_gate",
            "research_evidence_gate",
            "turn_contract_gate",
            "discussion_gate",
            "evidence_gate",
            "user_decision_gate",
        ):
            gate = (
                convergence_state.get(gate_id)
                if isinstance(convergence_state.get(gate_id), dict)
                else {}
            )
            item: dict[str, Any] = {
                "ready": gate.get("ready") is True,
                "status": gate.get("status"),
                "blocker_codes": [
                    str(blocker.get("code") or "")
                    for blocker in gate.get("blockers") or []
                    if isinstance(blocker, dict) and str(blocker.get("code") or "")
                ],
            }
            if gate_id == "data_gate":
                market_snapshot_complete = gate.get("market_snapshot_complete")
                item["market_snapshot_complete"] = (
                    market_snapshot_complete
                    if isinstance(market_snapshot_complete, bool)
                    else None
                )
                item["ready_market_symbols"] = sorted({
                    str(symbol)
                    for symbol in gate.get("ready_market_symbols") or []
                    if str(symbol)
                })
            elif gate_id == "turn_contract_gate":
                item.update({
                    "applicable": gate.get("applicable") is True,
                    "version": gate.get("version"),
                    "qualified_message_count": cls._safe_int(
                        gate.get("qualified_message_count")
                    ),
                })
                item["ready"] = bool(
                    item["ready"]
                    and item["applicable"]
                    and item["version"] == TURN_CONTRACT_VERSION
                )
            elif gate_id == "discussion_gate":
                item.update({
                    "successful_member_count": cls._safe_int(
                        gate.get("successful_member_count")
                    ),
                    "required_success_count": cls._safe_int(
                        gate.get("required_success_count")
                    ),
                })
                item["ready"] = bool(
                    item["ready"]
                    and item["successful_member_count"]
                    == STORAGE_QUALIFIED_ROLE_COUNT
                    and item["required_success_count"]
                    == STORAGE_QUALIFIED_ROLE_COUNT
                )
            elif gate_id == "evidence_gate":
                item.update({
                    "artifact_id": str(gate.get("artifact_id") or ""),
                    "artifact_version": cls._safe_int(gate.get("artifact_version")),
                    "evidence_count": cls._safe_int(gate.get("evidence_count")),
                    "unreviewed_evidence_count": cls._safe_int(
                        gate.get("unreviewed_evidence_count")
                    ),
                })
            elif gate_id == "user_decision_gate":
                item.update({
                    "decision_id": str(gate.get("decision_id") or ""),
                    "artifact_id": str(gate.get("artifact_id") or ""),
                    "artifact_version": cls._safe_int(gate.get("artifact_version")),
                    "ai_preferred_option_id": str(
                        gate.get("ai_preferred_option_id") or ""
                    ),
                    "selected_option_id": str(
                        gate.get("selected_option_id") or ""
                    ),
                    "preferred_option_id": str(
                        gate.get("selected_option_id") or ""
                    ),
                    "action": str(gate.get("action") or ""),
                })
            summary[gate_id] = item
        return summary

    @classmethod
    def _evidence_audit(
        cls,
        artifact: dict[str, Any] | None,
        evidence_gate: dict[str, Any],
    ) -> dict[str, Any]:
        review = (
            artifact.get("evidence_review")
            if isinstance(artifact, dict)
            and isinstance(artifact.get("evidence_review"), dict)
            else {}
        )
        relation_count = cls._safe_int(review.get("relation_count"))
        reviewed_count = cls._safe_int(review.get("reviewed_relation_count"))
        unreviewed_count = cls._safe_int(review.get("unreviewed_relation_count"))
        gate_count = cls._safe_int(evidence_gate.get("evidence_count"))
        gate_unreviewed = cls._safe_int(
            evidence_gate.get("unreviewed_evidence_count")
        )
        ready = bool(
            artifact
            and str(artifact.get("status") or "").upper() == "CONFIRMED"
            and evidence_gate.get("ready") is True
            and relation_count > 0
            and gate_count == relation_count
            and reviewed_count == relation_count
            and unreviewed_count == 0
            and gate_unreviewed == 0
            and review.get("confirmation_ready") is True
        )
        return {
            "ready": ready,
            "relation_count": relation_count,
            "reviewed_relation_count": reviewed_count,
            "unreviewed_relation_count": unreviewed_count,
            "gate_evidence_count": gate_count,
            "gate_unreviewed_evidence_count": gate_unreviewed,
            "confirmation_ready": review.get("confirmation_ready") is True,
        }

    @classmethod
    def _user_decision_binding(
        cls,
        artifact: dict[str, Any] | None,
        user_gate: dict[str, Any],
    ) -> dict[str, Any]:
        current = (
            artifact.get("user_decision")
            if isinstance(artifact, dict)
            and isinstance(artifact.get("user_decision"), dict)
            else None
        )
        artifact_id = str((artifact or {}).get("id") or "")
        artifact_version = cls._safe_int((artifact or {}).get("version"))
        decision_id = str((current or {}).get("id") or "")
        decision_version = str((current or {}).get("decision_version") or "")
        gate_decision_version = str(user_gate.get("decision_version") or "")
        decision_version_exact = bool(
            decision_version == USER_DECISION_VERSION
            and gate_decision_version == USER_DECISION_VERSION
        )
        action = str((current or {}).get("action") or "")
        expected_ai_preferred = preferred_option_id(artifact or {})
        actual_ai_preferred = str(
            (current or {}).get("ai_preferred_option_id") or ""
        )
        selected_option_id = str(
            (current or {}).get("selected_option_id") or ""
        )
        content = (
            artifact.get("content")
            if isinstance(artifact, dict)
            and isinstance(artifact.get("content"), dict)
            else {}
        )
        artifact_decision = (
            content.get("decision")
            if isinstance(content.get("decision"), dict)
            else {}
        )
        option_ids = {
            str(option.get("id") or "")
            for option in artifact_decision.get("options") or []
            if isinstance(option, dict) and str(option.get("id") or "")
        }
        selected_option_exact = bool(
            selected_option_id in option_ids
            if action == "support"
            else not selected_option_id
        )
        actual_sha256 = str(
            (current or {}).get("artifact_snapshot_sha256") or ""
        )
        artifact_snapshot_sha256_exact = bool(
            current
            and current.get("artifact_binding_integrity_ok") is True
            and re.fullmatch(r"[0-9a-f]{64}", actual_sha256)
        )
        ready = bool(
            artifact
            and current
            and current.get("is_current") is True
            and current.get("integrity_ok") is True
            and current.get("candidate_binding_integrity_ok") is True
            and decision_version_exact
            and decision_id
            and str(current.get("artifact_id") or "") == artifact_id
            and cls._safe_int(current.get("artifact_version")) == artifact_version
            and artifact_snapshot_sha256_exact
            and action in USER_DECISION_ACTIONS
            and bool(str(current.get("rationale") or "").strip())
            and actual_ai_preferred == expected_ai_preferred
            and selected_option_exact
            and user_gate.get("ready") is True
            and str(user_gate.get("decision_id") or "") == decision_id
            and str(user_gate.get("artifact_id") or "") == artifact_id
            and cls._safe_int(user_gate.get("artifact_version")) == artifact_version
            and str(user_gate.get("ai_preferred_option_id") or "")
            == expected_ai_preferred
            and str(user_gate.get("selected_option_id") or "")
            == selected_option_id
            and str(user_gate.get("action") or "") == action
        )
        return {
            "ready": ready,
            "decision_id": decision_id,
            "decision_version": decision_version,
            "gate_decision_version": gate_decision_version,
            "decision_version_exact": decision_version_exact,
            "action": action,
            "is_current": bool(current and current.get("is_current") is True),
            "artifact_id": artifact_id,
            "artifact_version": artifact_version,
            "decision_artifact_id": str((current or {}).get("artifact_id") or ""),
            "decision_artifact_version": cls._safe_int(
                (current or {}).get("artifact_version")
            ),
            "artifact_snapshot_sha256_exact": bool(
                artifact_snapshot_sha256_exact
            ),
            "ai_preferred_option_id": actual_ai_preferred,
            "selected_option_id": selected_option_id,
            "preferred_option_id": selected_option_id,
            "ai_preferred_option_exact": (
                actual_ai_preferred == expected_ai_preferred
            ),
            "selected_option_exact": selected_option_exact,
            "preferred_option_exact": selected_option_exact,
            "convergence_gate_exact": bool(
                user_gate.get("ready") is True
                and gate_decision_version == USER_DECISION_VERSION
                and str(user_gate.get("decision_id") or "") == decision_id
                and str(user_gate.get("artifact_id") or "") == artifact_id
                and cls._safe_int(user_gate.get("artifact_version"))
                == artifact_version
                and str(user_gate.get("ai_preferred_option_id") or "")
                == expected_ai_preferred
                and str(user_gate.get("selected_option_id") or "")
                == selected_option_id
                and str(user_gate.get("action") or "") == action
            ),
        }

    @classmethod
    def _paper_portfolio_gate_audit(
        cls,
        raw_gate: dict[str, Any],
        decision_binding: dict[str, Any],
    ) -> dict[str, Any]:
        """Project the canonical convergence portfolio gate into acceptance.

        The convergence service owns package-chain, exact revision, risk-gate,
        and execution-boundary validation.  Acceptance deliberately requires
        that stronger result instead of reimplementing a looser portfolio test.
        """

        action = str(decision_binding.get("action") or "")
        decision_id = str(decision_binding.get("decision_id") or "")
        gate_decision_id = str(raw_gate.get("decision_id") or "")
        blocker_codes = [
            str(item.get("code") or "")
            for item in raw_gate.get("blockers") or []
            if isinstance(item, dict) and str(item.get("code") or "")
        ]
        confirmed_count = cls._safe_int(raw_gate.get("confirmed_count"))
        canonical_gate_ready = bool(
            raw_gate.get("applicable") is True
            and raw_gate.get("ready") is True
            and str(raw_gate.get("status") or "") == "linked_confirmed"
            and confirmed_count >= 1
            and decision_id
            and gate_decision_id == decision_id
            and not blocker_codes
        )
        ready = bool(
            action == "support"
            and decision_binding.get("ready") is True
            and canonical_gate_ready
        )
        return {
            "ready": ready,
            "applicable": raw_gate.get("applicable") is True,
            "status": str(raw_gate.get("status") or ""),
            "user_decision_action": action,
            "decision_id": decision_id,
            "gate_decision_id": gate_decision_id,
            "decision_id_exact": bool(decision_id and gate_decision_id == decision_id),
            "confirmed_count": confirmed_count,
            "linked_count": cls._safe_int(raw_gate.get("linked_count")),
            "draft_count": cls._safe_int(raw_gate.get("draft_count")),
            "legacy_unlinked_count": cls._safe_int(
                raw_gate.get("legacy_unlinked_count")
            ),
            "blocker_codes": blocker_codes,
            "canonical_gate_ready": canonical_gate_ready,
            "current_decision_package_integrity_required": True,
            "exact_confirmed_revision_required": True,
            "risk_gate_ready_required": True,
            "execution_capability": "none",
            "live_trading_allowed": False,
        }

    @staticmethod
    def _compatibility_contract(meeting_reviewed: bool) -> dict[str, Any]:
        return {
            "previous_version": STORAGE_SAMPLE_ACCEPTANCE_PREVIOUS_VERSION,
            "strategy": "additive_fields_with_tightened_acceptance_semantics",
            "legacy_fields_preserved": True,
            "legacy_v1_meeting_acceptance_ready": meeting_reviewed is True,
            "legacy_v1_projection_authoritative": False,
            "semantic_change": (
                "v2 keeps the v1 field surface but acceptance_ready and "
                "research_sample_ready now require user support plus the "
                "canonical decision-package paper-portfolio gate."
            ),
        }

    @classmethod
    def _statistical_validation(
        cls,
        scorecard: dict[str, Any],
    ) -> dict[str, Any]:
        scorecard_version = str(scorecard.get("version") or "")
        scorecard_contract_ready = scorecard_version == OBSERVATION_SCORECARD_VERSION
        overall = (
            scorecard.get("overall")
            if scorecard_contract_ready and isinstance(scorecard.get("overall"), dict)
            else {}
        )
        independence = (
            scorecard.get("independence")
            if isinstance(scorecard.get("independence"), dict)
            else {}
        )
        sample_count = cls._safe_int(overall.get("sample_count"))
        scorecard_qualified = overall.get("qualified") is True
        ready = bool(
            sample_count >= OBSERVATION_MIN_SAMPLES
            and scorecard_qualified
        )
        return {
            "ready": ready,
            "blocks_operational_acceptance": False,
            "scorecard_version": scorecard_version,
            "required_scorecard_version": OBSERVATION_SCORECARD_VERSION,
            "scorecard_contract_ready": scorecard_contract_ready,
            "sample_count": sample_count,
            "minimum_samples": OBSERVATION_MIN_SAMPLES,
            "scorecard_qualified": scorecard_qualified,
            "mixed_methodology": overall.get("mixed_methodology") is True,
            "mixed_conditions": overall.get("mixed_conditions") is True,
            "descriptive_only": overall.get("descriptive_only") is True,
            "metric_label": str(overall.get("metric_label") or ""),
            "raw_resolved_count": cls._safe_int(
                independence.get("raw_resolved_count")
            ),
            "independent_sample_count": cls._safe_int(
                independence.get("independent_sample_count")
            ),
        }

    @staticmethod
    def _legacy_reasons(
        latest_round: dict[str, Any] | None,
        checkpoint: dict[str, Any] | None,
        checkpoint_version: int,
        contract_bundle: dict[str, Any],
    ) -> list[dict[str, str]]:
        if not latest_round:
            return []
        status = str(latest_round.get("status") or "").upper()
        terminal = status in {"COMPLETED", "PARTIAL"}
        reasons: list[dict[str, str]] = []
        if checkpoint and checkpoint_version < STORAGE_CHECKPOINT_VERSION:
            reasons.append({
                "code": "LEGACY_CHECKPOINT_VERSION",
                "detail": f"该历史轮次仅保存了 v{checkpoint_version} 检查点，低于要求的 v{STORAGE_CHECKPOINT_VERSION}。",
            })
        elif terminal and not checkpoint:
            reasons.append({
                "code": "LEGACY_CHECKPOINT_MISSING",
                "detail": "该历史终态轮次没有持久化检查点。",
            })
        if terminal and contract_bundle.get("applicable") is not True:
            reasons.append({
                "code": "LEGACY_TURN_CONTRACT_MISSING",
                "detail": f"该历史终态轮次早于 {TURN_CONTRACT_VERSION}，不得补造合同记录。",
            })
        return reasons

    @staticmethod
    def _blockers(
        checks: dict[str, dict[str, Any]],
        legacy_reasons: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        check_titles = {
            "latest_round": "尚无可验收轮次",
            "round_completed": "最新轮次尚未完整结束",
            "checkpoint_v7": "轮次检查点版本不合格",
            "frozen_capability_packs": "轮次能力包不完整",
            "turn_contract_v1": "结构化发言合同不完整",
            "twelve_unique_qualified_roles": "十二个独立角色未完整覆盖",
            "independent_data_guardian": "数据质量官未独立覆盖",
            "convergence_data_gate": "四股统一行情门未通过",
            "convergence_research_evidence_gate": "研究证据质量门未通过",
            "convergence_turn_contract_gate": "发言合同收敛门未通过",
            "convergence_discussion_gate": "讨论覆盖门未通过",
            "convergence_evidence_gate": "证据复核门未通过",
            "convergence_user_decision_gate": "用户决定门未通过",
            "single_round_artifact": "本轮会议产物不是唯一一份",
            "artifact_confirmed": "本轮会议产物尚未确认",
            "all_evidence_reviewed": "会议证据尚未全部复核",
            "candidate_decision": "候选方案决策板尚未完成",
            "exact_user_decision_binding": "用户决定未精确绑定当前版本",
            "supported_user_decision": "用户尚未支持当前候选",
            "paper_portfolio_gate": "当前决定包的纸面组合尚未通过风险复核",
            "convergence_research_ready": "收敛服务尚未确认研究样板就绪",
            "convergence_evaluation": "收敛状态无法复核",
        }
        blockers: list[dict[str, str]] = []
        seen: set[str] = set()
        for reason in legacy_reasons:
            code = str(reason.get("code") or "")
            if code and code not in seen:
                blockers.append({
                    "code": code,
                    "check_id": "legacy_round",
                    "title": "旧版轮次不计入当前验收",
                    "detail": str(reason.get("detail") or ""),
                })
                seen.add(code)
        for check_id, check in checks.items():
            if check.get("ready") is True:
                continue
            code = str(check.get("code") or "ACCEPTANCE_CHECK_FAILED")
            if code in seen:
                continue
            blockers.append({
                "code": code,
                "check_id": check_id,
                "title": check_titles.get(check_id, "样板验收条件未通过"),
                "detail": str(check.get("detail") or ""),
            })
            seen.add(code)
        return blockers

    @classmethod
    def _with_frontend_contract(
        cls,
        value: dict[str, Any],
    ) -> dict[str, Any]:
        """Add a compact UI contract without removing the detailed audit."""

        result = dict(value)
        checks = result.get("checks") if isinstance(result.get("checks"), dict) else {}
        latest_round = (
            result.get("latest_round")
            if isinstance(result.get("latest_round"), dict)
            else {}
        )
        latest_round_id = str(latest_round.get("id") or "")
        acceptance_ready = result.get("acceptance_ready") is True
        meeting_reviewed = result.get("meeting_reviewed") is True
        user_decision_action = str(result.get("user_decision_action") or "")
        legacy = result.get("legacy") is True

        failed_check_ids = {
            check_id
            for check_id, check in checks.items()
            if isinstance(check, dict) and check.get("ready") is not True
        }
        review_check_ids = {
            "artifact_confirmed",
            "all_evidence_reviewed",
            "convergence_evidence_gate",
            "convergence_user_decision_gate",
            "exact_user_decision_binding",
            "supported_user_decision",
            "paper_portfolio_gate",
            "convergence_research_ready",
        }
        if result.get("applicable") is not True:
            state = str(result.get("state") or "blocked")
        elif not latest_round_id:
            state = "no_round"
        elif legacy:
            state = "legacy"
        elif acceptance_ready:
            state = "accepted"
        elif meeting_reviewed and user_decision_action == "hold":
            state = "deferred"
        elif meeting_reviewed and user_decision_action == "return":
            state = "returned"
        elif failed_check_ids and failed_check_ids.issubset(review_check_ids):
            state = "review_required"
        else:
            state = "blocked"

        def checks_ready(*check_ids: str) -> bool:
            return bool(
                check_ids
                and all(
                    isinstance(checks.get(check_id), dict)
                    and checks[check_id].get("ready") is True
                    for check_id in check_ids
                )
            )

        convergence_gates = (
            result.get("convergence_gates")
            if isinstance(result.get("convergence_gates"), dict)
            else {}
        )
        data_gate = (
            convergence_gates.get("data_gate")
            if isinstance(convergence_gates.get("data_gate"), dict)
            else {}
        )
        ready_market_symbols = [
            str(item)
            for item in data_gate.get("ready_market_symbols") or []
            if str(item)
        ]
        market_current = len(set(ready_market_symbols))
        market_snapshot_complete = data_gate.get("market_snapshot_complete")
        market_snapshot_ready = bool(
            market_current == 4
            and (
                market_snapshot_complete is True
                if isinstance(market_snapshot_complete, bool)
                else checks_ready("convergence_data_gate")
            )
        )
        research_evidence_gate = (
            convergence_gates.get("research_evidence_gate")
            if isinstance(convergence_gates.get("research_evidence_gate"), dict)
            else {}
        )
        research_evidence_ready = checks_ready(
            "convergence_research_evidence_gate"
        )
        research_evidence_blocker_codes = [
            str(code)
            for code in research_evidence_gate.get("blocker_codes") or []
            if str(code)
        ]
        # Keep the v2 stages[].market_data aggregate for existing API consumers.
        market_ready = bool(market_snapshot_ready and research_evidence_ready)

        market_snapshot_gate = {
            "id": "market_snapshot",
            "state": "ready" if market_snapshot_ready else "blocked",
            "ready": market_snapshot_ready,
            "current": market_current,
            "required": 4,
            "detail": (
                "MU、SNDK、WDC、STX 的同轮 Futu 只读行情快照均已就绪。"
                if market_snapshot_ready
                else "Futu 同轮只读行情快照尚未通过完整性与时间质量检查。"
            ),
        }
        research_evidence_gate_contract = {
            "id": "research_evidence",
            "state": "ready" if research_evidence_ready else "blocked",
            "ready": research_evidence_ready,
            "current": 1 if research_evidence_ready else 0,
            "required": 1,
            "detail": (
                "当前轮次所需的官方研究证据已通过独立门禁。"
                if research_evidence_ready
                else "当前轮次所需的官方研究证据尚未通过；行情快照状态不受此项混淆。"
            ),
            "blocker_codes": research_evidence_blocker_codes,
        }

        role_audit = (
            result.get("role_audit")
            if isinstance(result.get("role_audit"), dict)
            else {}
        )
        discussion_current = cls._safe_int(role_audit.get("unique_member_count"))
        discussion_ready = bool(
            discussion_current == STORAGE_QUALIFIED_ROLE_COUNT
            and checks_ready(
                "round_completed",
                "checkpoint_v7",
                "frozen_capability_packs",
                "turn_contract_v1",
                "twelve_unique_qualified_roles",
                "independent_data_guardian",
                "convergence_turn_contract_gate",
                "convergence_discussion_gate",
            )
        )

        artifact_actual = (
            (checks.get("single_round_artifact") or {}).get("actual")
            if isinstance(checks.get("single_round_artifact"), dict)
            else {}
        )
        artifact_current = cls._safe_int(
            artifact_actual.get("count")
            if isinstance(artifact_actual, dict)
            else 0
        )
        artifact_ready = bool(
            artifact_current == 1
            and checks_ready(
                "single_round_artifact",
                "artifact_confirmed",
                "candidate_decision",
            )
        )

        evidence_actual = (
            (checks.get("all_evidence_reviewed") or {}).get("actual")
            if isinstance(checks.get("all_evidence_reviewed"), dict)
            else {}
        )
        evidence_current = cls._safe_int(
            evidence_actual.get("reviewed_relation_count")
            if isinstance(evidence_actual, dict)
            else 0
        )
        evidence_required = cls._safe_int(
            evidence_actual.get("relation_count")
            if isinstance(evidence_actual, dict)
            else 0
        )
        evidence_ready = bool(
            evidence_required > 0
            and evidence_current == evidence_required
            and checks_ready(
                "convergence_evidence_gate",
                "all_evidence_reviewed",
            )
        )

        user_decision_ready = checks_ready(
            "convergence_user_decision_gate",
            "exact_user_decision_binding",
        )
        paper_portfolio_gate = (
            result.get("paper_portfolio_gate")
            if isinstance(result.get("paper_portfolio_gate"), dict)
            else {}
        )
        paper_portfolio_ready = paper_portfolio_gate.get("ready") is True
        paper_portfolio_current = cls._safe_int(
            paper_portfolio_gate.get("confirmed_count")
        )
        if user_decision_action == "hold":
            paper_portfolio_state = "deferred"
            paper_portfolio_detail = (
                "用户已选择暂时保留；不会为该决定要求或自动建立纸面组合。"
            )
        elif user_decision_action == "return":
            paper_portfolio_state = "returned"
            paper_portfolio_detail = (
                "用户已退回当前方案；修订并重新确认前不会进入纸面组合验证。"
            )
        elif paper_portfolio_ready:
            paper_portfolio_state = "ready"
            paper_portfolio_detail = (
                "当前支持决定已精确关联通过风险门和只读边界的确认纸面组合。"
            )
        else:
            paper_portfolio_state = "pending"
            paper_portfolio_detail = (
                "当前支持决定尚缺完整决定包中的精确已确认、风险就绪纸面组合。"
            )
        stages: list[dict[str, Any]] = [
            {
                "id": "market_data",
                "state": "ready" if market_ready else "blocked",
                "ready": market_ready,
                "current": market_current,
                "required": 4,
                "detail": (
                    "Futu 四股行情快照与官方研究证据均已就绪。此项为 v2 兼容汇总。"
                    if market_ready
                    else (
                        "Futu 四股行情快照已就绪（4 / 4）；官方研究证据仍未通过。"
                        "此项为 v2 兼容汇总。"
                        if market_snapshot_ready
                        else (
                            "官方研究证据已就绪；Futu 四股行情快照仍未通过。"
                            "此项为 v2 兼容汇总。"
                            if research_evidence_ready
                            else (
                                "Futu 四股行情快照与官方研究证据尚未全部通过。"
                                "此项为 v2 兼容汇总。"
                            )
                        )
                    )
                ),
            },
            {
                "id": "discussion",
                "state": "ready" if discussion_ready else "blocked",
                "ready": discussion_ready,
                "current": discussion_current,
                "required": STORAGE_QUALIFIED_ROLE_COUNT,
                "detail": (
                    "十二名独立合格角色已完成本轮正式讨论。"
                    if discussion_ready
                    else "本轮尚未形成十二名独立合格角色的完整讨论账本。"
                ),
            },
            {
                "id": "artifact",
                "state": "ready" if artifact_ready else "pending",
                "ready": artifact_ready,
                "current": artifact_current,
                "required": 1,
                "detail": (
                    "本轮唯一会议产物已确认，并保留有效候选方案。"
                    if artifact_ready
                    else "本轮必须且只能有一份已确认、含有效候选方案的会议产物。"
                ),
            },
            {
                "id": "evidence",
                "state": "ready" if evidence_ready else "pending",
                "ready": evidence_ready,
                "current": evidence_current,
                "required": evidence_required,
                "detail": (
                    "会议产物中的全部证据关系均已复核。"
                    if evidence_ready
                    else "会议产物仍有证据关系未复核，或尚未形成可复核证据。"
                ),
            },
            {
                "id": "user_decision",
                "state": "ready" if user_decision_ready else "pending",
                "ready": user_decision_ready,
                "current": 1 if user_decision_ready else 0,
                "required": 1,
                "detail": (
                    "用户决定已精确绑定当前确认产物及其版本。"
                    if user_decision_ready
                    else "用户决定尚未精确绑定当前确认产物、版本和首选方案。"
                ),
            },
            {
                "id": "paper_portfolio",
                "state": paper_portfolio_state,
                "ready": paper_portfolio_ready,
                "current": paper_portfolio_current,
                "required": 1,
                "detail": paper_portfolio_detail,
            },
        ]

        statistics_detail = (
            result.get("statistical_validation")
            if isinstance(result.get("statistical_validation"), dict)
            else {}
        )
        sample_count = cls._safe_int(statistics_detail.get("sample_count"))
        minimum_samples = OBSERVATION_MIN_SAMPLES
        statistics_ready = statistics_detail.get("ready") is True
        stages.append({
            "id": "simulation",
            "state": "ready" if statistics_ready else "pending",
            "ready": statistics_ready,
            "current": sample_count,
            "required": minimum_samples,
            "detail": (
                "独立且可比的统计样本已达到展示门槛。"
                if statistics_ready
                else f"独立且可比的统计样本：{sample_count} / {minimum_samples}。"
            ),
        })

        next_actions: list[str] = []
        if state == "legacy":
            next_actions.append(
                "请按 v7 检查点和 turn_contract_v1 发起新一轮；历史轮次保持原样，不补造记录。"
            )
        elif state == "deferred":
            next_actions.append(
                "用户已暂时保留当前候选；等待条件变化或补充证据后再决定是否支持，不自动派生纸面组合。"
            )
        elif state == "returned":
            next_actions.append(
                "用户已退回当前候选；修订产物后必须重新完成证据确认与精确用户决定。"
            )
        for blocker in result.get("blockers") or []:
            if not isinstance(blocker, dict):
                continue
            if state in {"deferred", "returned"} and str(
                blocker.get("check_id") or ""
            ) in {
                "supported_user_decision",
                "paper_portfolio_gate",
                "convergence_research_ready",
            }:
                continue
            detail = str(blocker.get("detail") or "").strip()
            if detail and detail not in next_actions:
                next_actions.append(detail)
        if not statistics_ready:
            statistics_action = (
                f"累计至少 {minimum_samples} 个独立、可比、经用户确认且已到期的样本前，"
                "统计结果只作描述，不显示为胜率结论。"
            )
            if statistics_action not in next_actions:
                next_actions.append(statistics_action)

        result.update({
            "version": STORAGE_SAMPLE_ACCEPTANCE_VERSION,
            "state": state,
            "latest_round_id": latest_round_id or None,
            "meeting_reviewed": meeting_reviewed,
            "research_sample_ready": acceptance_ready,
            "user_decision_action": user_decision_action,
            "paper_portfolio_gate": paper_portfolio_gate,
            "market_snapshot_gate": market_snapshot_gate,
            "research_evidence_gate": research_evidence_gate_contract,
            "statistics": {
                "sample_count": sample_count,
                "minimum_samples": minimum_samples,
                "qualified": statistics_ready,
            },
            "stages": stages,
            "next_actions": next_actions,
        })
        return result

    @classmethod
    def _missing_room_result(cls, room_id: str) -> dict[str, Any]:
        statistics = cls._statistical_validation({})
        result = {
            "schema_version": STORAGE_SAMPLE_ACCEPTANCE_VERSION,
            "room_id": room_id,
            "applicable": False,
            "state": "blocked",
            "acceptance_ready": False,
            "meeting_reviewed": False,
            "research_sample_ready": False,
            "user_decision_action": "",
            "paper_portfolio_gate": cls._paper_portfolio_gate_audit({}, {}),
            "blocked": True,
            "legacy": False,
            "latest_round": None,
            "checks": {
                "room_exists": {
                    "ready": False,
                    "required": True,
                    "code": "ROOM_NOT_FOUND",
                    "expected": "persisted room",
                    "actual": None,
                    "detail": "房间不存在；验收器不会自动创建房间。",
                },
            },
            "blockers": [{
                "code": "ROOM_NOT_FOUND",
                "check_id": "room_exists",
                "detail": "房间不存在；验收器不会自动创建房间。",
            }],
            "role_audit": {},
            "convergence_gates": {},
            "statistical_validation": statistics,
            "statistical_validation_ready": False,
            "compatibility": cls._compatibility_contract(False),
            "read_only": True,
            "provider_calls": 0,
            "market_calls": 0,
            "historical_backfill_performed": False,
            "execution_capability": "none",
            "live_trading_allowed": False,
            "can_autonomously_decide": False,
        }
        return cls._with_frontend_contract(result)

    @classmethod
    def _not_applicable_result(
        cls,
        room_id: str,
        room: dict[str, Any],
        statistics: dict[str, Any],
    ) -> dict[str, Any]:
        actual_template = str(room.get("template_id") or "")
        actual_capabilities = [
            str(item) for item in room.get("capabilities") or [] if str(item)
        ]
        result = {
            "schema_version": STORAGE_SAMPLE_ACCEPTANCE_VERSION,
            "room_id": room_id,
            "applicable": False,
            "state": "not_applicable",
            "acceptance_ready": False,
            "meeting_reviewed": False,
            "research_sample_ready": False,
            "user_decision_action": "",
            "paper_portfolio_gate": cls._paper_portfolio_gate_audit({}, {}),
            "blocked": False,
            "legacy": False,
            "latest_round": None,
            "checks": {
                "storage_room_scope": {
                    "ready": False,
                    "required": True,
                    "code": "STORAGE_ROOM_REQUIRED",
                    "expected": "market.storage.readonly",
                    "actual": {
                        "template_id": actual_template or None,
                        "capabilities": actual_capabilities,
                    },
                    "detail": "当前房间未启用美国存储产业只读研究能力包。",
                },
            },
            "blockers": [],
            "role_audit": {},
            "convergence_gates": {},
            "statistical_validation": statistics,
            "statistical_validation_ready": statistics["ready"],
            "compatibility": cls._compatibility_contract(False),
            "read_only": True,
            "provider_calls": 0,
            "market_calls": 0,
            "historical_backfill_performed": False,
            "execution_capability": "none",
            "live_trading_allowed": False,
            "can_autonomously_decide": False,
        }
        return cls._with_frontend_contract(result)

    @staticmethod
    def _safe_int(value: Any) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0


__all__ = [
    "STORAGE_SAMPLE_ACCEPTANCE_LEGACY_VERSION",
    "STORAGE_SAMPLE_ACCEPTANCE_PREVIOUS_VERSION",
    "STORAGE_SAMPLE_ACCEPTANCE_VERSION",
    "StorageSampleAcceptance",
]
