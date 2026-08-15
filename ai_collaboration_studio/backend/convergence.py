from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .store import (
    ARTIFACT_SECTIONS,
    OBSERVATION_MIN_SAMPLES,
    OBSERVATION_SCORECARD_VERSION,
    StudioStore,
)
from .capability_packs import room_has_capability
from .market.futu_readonly import (
    STORAGE_SYMBOLS as STORAGE_SYMBOL_ORDER,
    validate_storage_quote_snapshot,
)
from .market.earnings_pack_contract import covered_official_earnings_pack_symbols
from .market.manual_official_evidence import (
    MANUAL_SUBSTITUTION_STATE,
    effective_source_errors,
    trusted_manual_substitution_claimed,
    validate_manual_official_evidence,
)
from .turn_contract import (
    CANDIDATE_LINEAGE_VERSION,
    CANDIDATE_RISK_REVIEW_VERSION,
    TURN_CONTRACT_VERSION,
)
from .turn_contract_artifact import project_turn_contract_artifact
from .workflow_policy import member_matches_requirement, policy_from_json


STORAGE_SYMBOLS = set(STORAGE_SYMBOL_ORDER)
STORAGE_TECHNICAL_MAX_AGE_DAYS = 7

class ConvergenceService:
    """Compute an explainable, read-only convergence view from persisted room state.

    Discussion coverage, research-data quality, evidence review, and simulation
    validation deliberately stay separate. A model can close only after the
    applicable deterministic gates pass, and it can never turn that into an
    autonomous final decision or an execution instruction.
    """

    def __init__(self, store: StudioStore) -> None:
        self.store = store

    def market_preflight(
        self,
        snapshot: dict[str, Any],
        market_snapshot: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Evaluate a prospective market snapshot without reading or writing round state.

        This delegates quote and safety validation to the same data gate used by
        convergence. Full research-evidence quality remains a discussion-time gate:
        a frozen defect must be explained in an auditable partial round, while it
        still prevents the host from declaring convergence.
        """
        room = snapshot.get("room") or {}
        template_id = str(room.get("template_id") or "open_collaboration")
        applicable = room_has_capability(room, "market.storage.readonly")
        # Round admission deliberately checks the quote/safety envelope only. The
        # full research bundle is evaluated during discussion so a data-quality
        # member can explain a degraded frozen snapshot instead of silently losing
        # the round before any auditable discussion exists.
        gate = self._data_gate(
            snapshot,
            [],
            market_snapshot,
            applicable,
            enforce_research_quality=False,
        )
        return {
            "applicable": applicable,
            "template_id": template_id,
            **gate,
        }

    def workflow_configuration_preflight(
        self,
        snapshot: dict[str, Any],
        *,
        workflow_policy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Check whether a round can ever satisfy its frozen workflow policy.

        This gate checks the room's configured, enabled roster rather than a temporary
        per-round selection. It deliberately does not count historical messages or
        call a model, so callers can reject structurally impossible rooms before
        market/provider preflight and before any round is persisted.
        """
        room = snapshot.get("room") or {}
        configured_members = [
            member for member in snapshot.get("members") or [] if member.get("enabled")
        ]
        policy = (
            policy_from_json(
                workflow_policy,
                str(room.get("template_id") or "open_collaboration"),
            )
            if workflow_policy is not None
            else policy_from_json(
                room.get("workflow_policy"),
                str(room.get("template_id") or "open_collaboration"),
            )
        )
        role_coverage = self._role_coverage(
            configured_members,
            [],
            policy["required_coverage"],
        )
        stage_coverage = self._stage_coverage(
            configured_members,
            [],
            policy["minimum_stage_coverage"],
        )
        required_success_count = int(policy["minimum_successful_members"])
        blockers: list[dict[str, str]] = []
        if len(configured_members) < required_success_count:
            blockers.append(self._issue(
                "WORKFLOW_MEMBER_CAPACITY_MISSING",
                "启用成员不足",
                (
                    f"当前只有 {len(configured_members)} 位启用成员，政策要求至少 "
                    f"{required_success_count} 位不同成员成功发言。"
                ),
                "workflow",
            ))
        for item in stage_coverage:
            if item["configured_count"] < item["required_count"]:
                blockers.append(self._issue(
                    f"WORKFLOW_STAGE_{item['id'].upper()}_MISSING",
                    f"缺少“{item['label']}”阶段成员",
                    (
                        f"已配置 {item['configured_count']} 位，政策要求 {item['required_count']} 位；"
                        "请调整成员流程阶段或房间政策后再开始。"
                    ),
                    "workflow",
                ))
        for item in role_coverage:
            if item["configured_count"] < item["required_count"]:
                blockers.append(self._issue(
                    f"WORKFLOW_ROLE_{item['id'].upper()}_MISSING",
                    f"缺少{item['label']}角色",
                    (
                        f"已配置 {item['configured_count']} 位，政策要求 {item['required_count']} 位；"
                        "请调整成员立场/能力或房间政策后再开始。"
                    ),
                    "workflow",
                ))
        return {
            "ready": not blockers,
            "configured_member_count": len(configured_members),
            "required_success_count": required_success_count,
            "stage_coverage": stage_coverage,
            "role_coverage": role_coverage,
            "blockers": blockers,
            "label": "讨论配置可执行" if not blockers else "讨论配置存在缺口",
        }

    def project_workspace_snapshot(
        self,
        room_id: str,
        *,
        snapshot: dict[str, Any] | None = None,
        frozen: bool = False,
    ) -> dict[str, Any]:
        """Return a bounded, prompt-safe summary of the latest project artifact.

        The snapshot deliberately carries counts and missing-field descriptions,
        not user-authored artifact text. A round freezes this object so later
        artifact edits cannot silently change the moderator's task mid-round.
        """
        room_snapshot = snapshot or self.store.room_snapshot(room_id)
        if not room_snapshot:
            raise ValueError("房间不存在")
        room = room_snapshot.get("room") or {}
        applicable = room_has_capability(room, "decision.project_recommendation")
        artifacts = self.store.list_artifacts(room_id) if applicable else []
        return self._project_workspace_snapshot(
            artifacts[0] if artifacts else None,
            applicable=applicable,
            frozen=frozen,
        )

    @staticmethod
    def project_workspace_prompt_context(workspace: dict[str, Any] | None) -> str:
        if not isinstance(workspace, dict) or not workspace.get("applicable"):
            return ""
        gaps = [item for item in workspace.get("gaps") or [] if isinstance(item, dict)]
        lines = [
            "[项目研究工作区缺口快照]",
            (
                f"来源产物：{workspace.get('artifact_id') or '尚未创建'}"
                f" · v{workspace.get('artifact_version') or 0}"
                f" · {workspace.get('artifact_status') or 'NONE'}"
            ),
            (
                f"需求 {workspace.get('requirement_count') or 0} 项；"
                f"风险 {workspace.get('risk_count') or 0} 项；"
                f"候选方案 {workspace.get('option_count') or 0} 项。"
            ),
        ]
        if gaps:
            lines.append("当前应补齐的缺口：")
            for item in gaps[:8]:
                lines.append(
                    f"- {item.get('code')}: {item.get('title')}；{item.get('detail')}；"
                    f"适配职责={','.join(item.get('target_capabilities') or []) or 'facilitation'}"
                )
        else:
            lines.append("当前结构字段完整；仍需基于本轮新证据复核，不得把旧产物直接当作最终结论。")
        lines.append("该快照只用于安排补证与复核，不代表产物内容已被系统证实，也不授权任何外部执行。")
        return "\n".join(lines)

    @staticmethod
    def legacy_project_workspace_snapshot() -> dict[str, Any]:
        gap = {
            "code": "PROJECT_WORKSPACE_CHECKPOINT_MISSING",
            "title": "旧轮次未冻结项目工作区",
            "detail": "本轮继续沿用原有共享上下文，不读取暂停后变化的项目产物；下一新轮会冻结最新工作区。",
            "target_capabilities": ["facilitation"],
        }
        return {
            "applicable": True,
            "frozen": True,
            "artifact_id": "",
            "artifact_version": 0,
            "artifact_status": "LEGACY_UNAVAILABLE",
            "requirement_count": 0,
            "confirmed_requirement_count": 0,
            "pending_requirement_count": 0,
            "missing_acceptance_count": 0,
            "risk_count": 0,
            "blocking_open_risk_count": 0,
            "missing_risk_trigger_count": 0,
            "untreated_risk_count": 0,
            "option_count": 0,
            "matrix_missing_dimensions": [],
            "preferred_option_ready": False,
            "gaps": [gap],
            "focus": gap,
            "ready": False,
            "label": "旧轮次没有可用的冻结项目工作区",
        }

    @staticmethod
    def _project_workspace_snapshot(
        artifact: dict[str, Any] | None,
        *,
        applicable: bool,
        frozen: bool,
    ) -> dict[str, Any]:
        base = {
            "applicable": bool(applicable),
            "frozen": bool(frozen),
            "artifact_id": "",
            "artifact_version": 0,
            "artifact_status": "NONE",
            "requirement_count": 0,
            "confirmed_requirement_count": 0,
            "pending_requirement_count": 0,
            "missing_acceptance_count": 0,
            "risk_count": 0,
            "blocking_open_risk_count": 0,
            "missing_risk_trigger_count": 0,
            "untreated_risk_count": 0,
            "option_count": 0,
            "matrix_missing_dimensions": [],
            "preferred_option_ready": False,
            "gaps": [],
            "focus": None,
            "ready": not applicable,
            "label": "当前房间不使用项目研究工作区" if not applicable else "尚未形成项目研究工作区",
        }
        if not applicable:
            return base
        if not artifact:
            gap = {
                "code": "PROJECT_WORKSPACE_MISSING",
                "title": "尚未创建项目研究工作区",
                "detail": "先由证据研究与方案整合角色建立需求、风险和至少两个候选方案。",
                "target_capabilities": ["evidence_review", "decision_synthesis"],
            }
            return {**base, "gaps": [gap], "focus": gap}

        content = artifact.get("content") if isinstance(artifact.get("content"), dict) else {}
        requirements = [item for item in content.get("requirements") or [] if isinstance(item, dict)]
        risks = [item for item in content.get("risks") or [] if isinstance(item, dict)]
        decision = content.get("decision") if isinstance(content.get("decision"), dict) else {}
        options = [item for item in decision.get("options") or [] if isinstance(item, dict)]
        confirmed_requirements = [
            item for item in requirements if str(item.get("status") or "pending") == "confirmed"
        ]
        pending_requirements = [
            item for item in requirements
            if str(item.get("status") or "pending") in {"assumption", "pending"}
        ]
        missing_acceptance = [
            item for item in confirmed_requirements
            if not str(item.get("acceptance_criteria") or "").strip()
        ]
        blocking_open_risks = [
            item for item in risks
            if item.get("blocking") is not False
            and str(item.get("status") or "open") in {"open", "monitoring"}
        ]
        missing_risk_triggers = [
            item for item in risks if not str(item.get("trigger") or "").strip()
        ]
        untreated_risks = [
            item for item in risks
            if str(item.get("status") or "open") in {"mitigated", "accepted"}
            and not str(item.get("mitigation") or "").strip()
        ]
        required_dimensions = ("value", "cost", "timeline", "dependencies", "reversibility")
        missing_dimensions = sorted({
            dimension
            for option in options
            for dimension in required_dimensions
            if (
                not option.get(dimension)
                if dimension == "dependencies"
                else not str(option.get(dimension) or "").strip()
                or (dimension == "reversibility" and str(option.get(dimension) or "unknown") == "unknown")
            )
        })
        option_ids = {str(item.get("id") or "") for item in options if str(item.get("id") or "")}
        preferred_ready = (
            str(decision.get("status") or "undecided") == "candidate"
            and str(decision.get("preferred_option_id") or "") in option_ids
            and bool(str(decision.get("rationale") or "").strip())
        )
        gaps: list[dict[str, Any]] = []

        def add_gap(code: str, title: str, detail: str, capabilities: list[str]) -> None:
            gaps.append({
                "code": code,
                "title": title,
                "detail": detail,
                "target_capabilities": capabilities,
            })

        if not requirements:
            add_gap(
                "PROJECT_REQUIREMENTS_MISSING",
                "需求地图为空",
                "需要区分用户原话、已确认需求、工作假设和待补证据。",
                ["evidence_review"],
            )
        elif pending_requirements:
            add_gap(
                "PROJECT_REQUIREMENT_EVIDENCE_GAP",
                "仍有需求或假设待确认",
                f"共有 {len(pending_requirements)} 项仍是工作假设或待补证据。",
                ["evidence_review"],
            )
        if missing_acceptance:
            add_gap(
                "PROJECT_ACCEPTANCE_MISSING",
                "已确认需求缺少验收条件",
                f"共有 {len(missing_acceptance)} 项已确认需求没有可测试验收条件。",
                ["evidence_review", "decision_synthesis"],
            )
        if not risks:
            add_gap(
                "PROJECT_RISKS_MISSING",
                "风险登记为空",
                "需要独立寻找失败路径、触发信号、影响与缓解动作。",
                ["critical_review"],
            )
        if missing_risk_triggers:
            add_gap(
                "PROJECT_RISK_TRIGGER_MISSING",
                "风险缺少触发信号",
                f"共有 {len(missing_risk_triggers)} 项风险没有可观察触发信号。",
                ["critical_review"],
            )
        if untreated_risks:
            add_gap(
                "PROJECT_RISK_TREATMENT_MISSING",
                "已处理风险缺少处置说明",
                f"共有 {len(untreated_risks)} 项风险标记为已处理，但没有缓解动作或接受理由。",
                ["critical_review"],
            )
        if blocking_open_risks:
            add_gap(
                "PROJECT_BLOCKING_RISK_OPEN",
                "阻断性风险仍然开放",
                f"共有 {len(blocking_open_risks)} 项阻断风险处于待处理或监控状态。",
                ["critical_review"],
            )
        if len(options) < 2:
            add_gap(
                "PROJECT_OPTIONS_INSUFFICIENT",
                "候选方案不足",
                f"当前只有 {len(options)} 个候选方案，需要至少两个方向不同的真实方案。",
                ["simulation_planning"],
            )
        if missing_dimensions:
            add_gap(
                "PROJECT_MATRIX_INCOMPLETE",
                "方案共同维度不完整",
                f"仍缺少共同维度：{', '.join(missing_dimensions)}。",
                ["decision_synthesis"],
            )
        if len(options) >= 2 and not preferred_ready:
            add_gap(
                "PROJECT_RECOMMENDATION_INCOMPLETE",
                "候选首选或理由不完整",
                "需要记录条件化首选、选择理由和主要保留项，并交由用户决定。",
                ["decision_synthesis"],
            )

        return {
            **base,
            "artifact_id": str(artifact.get("id") or "")[:80],
            "artifact_version": max(0, int(artifact.get("version") or 0)),
            "artifact_status": str(artifact.get("status") or "DRAFT").upper()[:24],
            "requirement_count": len(requirements),
            "confirmed_requirement_count": len(confirmed_requirements),
            "pending_requirement_count": len(pending_requirements),
            "missing_acceptance_count": len(missing_acceptance),
            "risk_count": len(risks),
            "blocking_open_risk_count": len(blocking_open_risks),
            "missing_risk_trigger_count": len(missing_risk_triggers),
            "untreated_risk_count": len(untreated_risks),
            "option_count": len(options),
            "matrix_missing_dimensions": missing_dimensions,
            "preferred_option_ready": preferred_ready,
            "gaps": gaps,
            "focus": gaps[0] if gaps else None,
            "ready": not gaps,
            "label": "项目研究结构完整" if not gaps else f"项目工作区仍有 {len(gaps)} 项缺口",
        }

    def evaluate(
        self,
        room_id: str,
        *,
        round_id: str = "",
        snapshot: dict[str, Any] | None = None,
        runtime: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        room_snapshot = snapshot or self.store.room_snapshot(room_id)
        if not room_snapshot:
            raise ValueError("房间不存在")

        runtime_state = runtime if isinstance(runtime, dict) else {}
        room = room_snapshot.get("room") or {}
        latest_round = room_snapshot.get("latest_round") or {}
        active_round_id = str(round_id or latest_round.get("id") or "")
        active_round = (
            self.store.get_round(room_id, active_round_id)
            if active_round_id
            else None
        ) or latest_round
        turn_contract_bundle = (
            self.store.round_turn_contract_bundle(room_id, active_round_id)
            if active_round_id
            else {"applicable": False, "valid": True, "messages": [], "issues": []}
        )
        turn_contract_required = turn_contract_bundle.get("applicable") is True
        turn_contract_version = (
            TURN_CONTRACT_VERSION
            if turn_contract_required
            else str(active_round.get("turn_contract_version") or "")
        )
        candidate_risk_review_version = str(
            turn_contract_bundle.get("candidate_risk_review_version")
            or active_round.get("candidate_risk_review_version")
            or ""
        ).strip()
        candidate_risk_review_required = (
            candidate_risk_review_version == CANDIDATE_RISK_REVIEW_VERSION
        )
        enabled_members = [member for member in room_snapshot.get("members") or [] if member.get("enabled")]
        members_by_id = {str(member.get("id") or ""): member for member in enabled_members}
        all_round_messages = (
            self.store.round_messages(room_id, active_round_id, limit=400)
            if turn_contract_required and active_round_id
            else room_snapshot.get("messages") or []
        )
        round_messages = [
            message for message in all_round_messages
            if active_round_id
            and message.get("round_id") == active_round_id
            and message.get("sender_type") == "ai"
        ]
        formal_round_ai_messages = [
            message
            for message in round_messages
            if message.get("is_formal_round_turn") is True
        ]
        if turn_contract_required:
            round_messages = [
                message
                for message in turn_contract_bundle.get("messages") or []
                if isinstance(message, dict)
            ]
        successful_member_ids = {
            str(message.get("sender_id") or "") for message in round_messages if str(message.get("sender_id") or "")
        }

        checkpoint_state: dict[str, Any] = {}
        if active_round_id:
            checkpoint = self.store.get_round_checkpoint(room_id, active_round_id)
            checkpoint_state = (checkpoint or {}).get("state") or {}
            checkpoint_success_ids = {
                str(member_id)
                for member_id in checkpoint_state.get("successful_member_ids") or []
                if str(member_id)
            }
            if turn_contract_required:
                successful_member_ids.intersection_update(checkpoint_success_ids)
            else:
                successful_member_ids.update(checkpoint_success_ids)
        if isinstance(checkpoint_state.get("room_capabilities"), list):
            room = {
                **room,
                "capability_pack_ids": list(checkpoint_state.get("capability_pack_ids") or []),
                "capabilities": [
                    str(item) for item in checkpoint_state.get("room_capabilities") or [] if str(item)
                ],
            }

        runtime_success_ids = {
            str(member_id)
            for member_id in runtime_state.get("successful_member_ids") or []
            if str(member_id)
        }
        if turn_contract_required:
            if isinstance(runtime_state.get("successful_member_ids"), (list, tuple, set)):
                successful_member_ids.intersection_update(runtime_success_ids)
        else:
            successful_member_ids.update(runtime_success_ids)
        workflow_policy = policy_from_json(
            checkpoint_state.get("workflow_policy")
            if checkpoint_state.get("workflow_policy")
            else room.get("workflow_policy"),
            str(room.get("template_id") or "open_collaboration"),
        )
        successful_member_versions: list[dict[str, Any]] = []
        versioned_ids: set[str] = set()
        for message in round_messages:
            member_id = str(message.get("sender_id") or "")
            if member_id not in members_by_id:
                continue
            member_version = int(message.get("member_version") or 0)
            version_snapshot = (
                self.store.get_member_version(room_id, member_id, member_version)
                if member_version > 0
                else None
            )
            successful_member_versions.append(version_snapshot or members_by_id[member_id])
            versioned_ids.add(member_id)
        for member_id in successful_member_ids:
            if member_id in members_by_id and member_id not in versioned_ids:
                successful_member_versions.append(members_by_id[member_id])

        is_storage = room_has_capability(room, "market.storage.readonly")
        is_project = room_has_capability(room, "decision.project_recommendation")
        successful_enabled_ids = successful_member_ids.intersection(members_by_id)
        role_coverage = self._role_coverage(
            enabled_members,
            successful_member_versions,
            workflow_policy["required_coverage"],
        )
        stage_coverage = self._stage_coverage(
            enabled_members,
            successful_member_versions,
            workflow_policy["minimum_stage_coverage"],
        )
        required_success_count = int(workflow_policy["minimum_successful_members"])
        coverage_ready = (
            bool(active_round_id)
            and all(item["ready"] for item in role_coverage)
            and all(item["ready"] for item in stage_coverage)
        )
        count_ready = len(successful_enabled_ids) >= required_success_count
        objective_ready = bool(str((active_round if active_round_id else room).get("objective") or room.get("objective") or "").strip())
        turn_contract_gate = {
            "applicable": turn_contract_required,
            "version": turn_contract_version or None,
            "integrity_issues": [
                str(item)
                for item in turn_contract_bundle.get("issues") or []
                if str(item)
            ] if turn_contract_required else [],
            "formal_message_count": len(formal_round_ai_messages),
            "qualified_message_count": len(round_messages) if turn_contract_required else 0,
            "unqualified_message_count": (
                max(0, len(formal_round_ai_messages) - len(round_messages))
                if turn_contract_required
                else 0
            ),
            "ready": (
                turn_contract_bundle.get("valid") is True
                and
                bool(round_messages)
                and len(round_messages) == len(formal_round_ai_messages)
                if turn_contract_required
                else True
            ),
            "label": (
                "正式发言合同均已核验"
                if turn_contract_required
                and turn_contract_bundle.get("valid") is True
                and round_messages
                and len(round_messages) == len(formal_round_ai_messages)
                else "正式发言合同尚未全部核验"
                if turn_contract_required
                else "本轮沿用历史发言规则"
            ),
        }
        candidate_lineage_gate: dict[str, Any] = {
            "applicable": False,
            "version": None,
            "ready": True,
            "status": "not_required",
            "decision_message_id": "",
            "referenced_candidate_ids": [],
            "candidate_count": 0,
            "blockers": [],
            "focus": None,
            "label": "本轮沿用历史候选规则",
        }
        candidate_risk_review_gate: dict[str, Any] = {
            "applicable": False,
            "version": None,
            "ready": True,
            "status": "not_required",
            "decision_message_id": "",
            "candidate_count": 0,
            "reviewed_candidate_count": 0,
            "review_count": 0,
            "stale_review_count": 0,
            "support_count": 0,
            "challenge_count": 0,
            "reject_count": 0,
            "blockers": [],
            "focus": None,
            "label": "本轮沿用历史候选风控规则",
        }
        if turn_contract_required:
            try:
                projection = project_turn_contract_artifact(
                    round_messages,
                    member_resolver=lambda member_id, version: self.store.get_member_version(
                        room_id,
                        member_id,
                        version,
                    ),
                    candidate_risk_review_required=candidate_risk_review_required,
                )
                lineage = (
                    projection.get("candidate_lineage")
                    if isinstance(projection.get("candidate_lineage"), dict)
                    else {}
                )
                lineage_issues = [
                    issue
                    for issue in lineage.get("issues") or []
                    if isinstance(issue, dict)
                ]
                lineage_blockers = [
                    self._issue(
                        str(issue.get("code") or "CANDIDATE_LINEAGE_INVALID"),
                        "候选对象谱系未闭合",
                        str(issue.get("message") or "候选对象谱系校验失败。"),
                        "candidate_lineage",
                    )
                    for issue in lineage_issues
                ]
                lineage_focus = None
                if lineage_issues:
                    first_issue = lineage_issues[0]
                    issue_code = str(first_issue.get("code") or "")
                    candidate_generation_focus = (
                        issue_code
                        == "CANDIDATE_LINEAGE_COMPARISON_INSUFFICIENT"
                    )
                    lineage_focus = {
                        "code": issue_code or "CANDIDATE_LINEAGE_INCOMPLETE",
                        "title": (
                            "候选方案数量不足"
                            if candidate_generation_focus
                            else "候选决策引用尚未闭合"
                        ),
                        "detail": str(
                            first_issue.get("message")
                            or "由匹配角色在本轮补齐候选谱系或决策引用。"
                        ),
                        "target_capabilities": [
                            "simulation_planning"
                            if candidate_generation_focus
                            else "decision_synthesis"
                        ],
                        "target_stances": [
                            "trader"
                            if candidate_generation_focus
                            else "portfolio_manager"
                        ],
                        "repair_scope": "in_round",
                        "coverage_mode": "until_resolved",
                        "routing_priority": (
                            "after_project"
                            if issue_code == "CANDIDATE_LINEAGE_DECISION_MISSING"
                            else "candidate_repair"
                        ),
                    }
                candidate_lineage_gate = {
                    "applicable": True,
                    "version": str(lineage.get("version") or CANDIDATE_LINEAGE_VERSION),
                    "ready": lineage.get("ready") is True and not lineage_blockers,
                    "status": str(lineage.get("status") or "blocked"),
                    "decision_message_id": str(lineage.get("decision_message_id") or ""),
                    "referenced_candidate_ids": [
                        str(candidate_id)
                        for candidate_id in lineage.get("referenced_candidate_ids") or []
                        if str(candidate_id)
                    ],
                    "candidate_count": len([
                        candidate
                        for candidate in lineage.get("candidates") or []
                        if isinstance(candidate, dict)
                    ]),
                    "blockers": lineage_blockers,
                    "focus": lineage_focus,
                    "label": (
                        "决策仅引用决策前候选快照"
                        if lineage.get("ready") is True and not lineage_blockers
                        else "候选对象谱系阻止收敛"
                    ),
                }
                review_projection = (
                    projection.get("candidate_risk_reviews")
                    if isinstance(projection.get("candidate_risk_reviews"), dict)
                    else {}
                )
                if candidate_risk_review_required:
                    review_issues = [
                        issue
                        for issue in review_projection.get("issues") or []
                        if isinstance(issue, dict)
                    ]
                    review_blockers = [
                        self._issue(
                            str(issue.get("code") or "CANDIDATE_RISK_REVIEW_INVALID"),
                            "候选精确版本风控未闭合",
                            str(issue.get("message") or "候选风险复核版本校验失败。"),
                            "candidate_risk_review",
                        )
                        for issue in review_issues
                    ]
                    action_counts = (
                        review_projection.get("action_counts")
                        if isinstance(review_projection.get("action_counts"), dict)
                        else {}
                    )
                    review_ready = (
                        review_projection.get("ready") is True
                        and not review_blockers
                    )
                    focus = None
                    if not review_ready:
                        risk_issue_codes = {
                            "CANDIDATE_RISK_REVIEW_REWRITE_FORBIDDEN",
                            "CANDIDATE_RISK_REVIEW_STALE_REFERENCE",
                            "CANDIDATE_RISK_REVIEW_STALE",
                            "CANDIDATE_RISK_REVIEW_RESPONSE_TARGET_MISSING",
                            "CANDIDATE_RISK_REVIEW_MISSING",
                        }
                        first_issue = next(
                            (
                                issue
                                for issue in review_issues
                                if str(issue.get("code") or "") in risk_issue_codes
                            ),
                            review_issues[0] if review_issues else {},
                        )
                        issue_code = str(first_issue.get("code") or "")
                        decision_focus = issue_code in {
                            "CANDIDATE_RISK_REVIEW_DECISION_MISSING",
                            "CANDIDATE_RISK_REVIEW_DECISION_REFERENCE_MISSING",
                            "CANDIDATE_RISK_REVIEW_DECISION_REVISIT_REQUIRED",
                            "CANDIDATE_RISK_REVIEW_TARGET_MISSING",
                        }
                        focus = {
                            "code": issue_code or "CANDIDATE_RISK_REVIEW_INCOMPLETE",
                            "title": (
                                "候选版本变化后需要重新决策"
                                if decision_focus
                                else "候选当前版本需要风险复核"
                            ),
                            "detail": str(
                                first_issue.get("message")
                                or "由匹配角色补齐候选当前版本的结构化复核。"
                            ),
                            "target_capabilities": [
                                "decision_synthesis" if decision_focus else "risk_review"
                            ],
                            "target_stances": [
                                "portfolio_manager" if decision_focus else "risk"
                            ],
                        }
                    target_count = int(
                        review_projection.get("target_candidate_count") or 0
                    )
                    candidate_risk_review_gate = {
                        "applicable": True,
                        "version": CANDIDATE_RISK_REVIEW_VERSION,
                        "ready": review_ready,
                        "status": str(review_projection.get("status") or "blocked"),
                        "decision_message_id": str(
                            review_projection.get("decision_message_id") or ""
                        ),
                        "candidate_count": target_count or len([
                            candidate
                            for candidate in lineage.get("candidates") or []
                            if isinstance(candidate, dict)
                        ]),
                        "reviewed_candidate_count": int(
                            review_projection.get("reviewed_candidate_count") or 0
                        ),
                        "review_count": int(
                            review_projection.get("review_count") or 0
                        ),
                        "stale_review_count": int(
                            review_projection.get("stale_review_count") or 0
                        ),
                        "support_count": int(action_counts.get("support") or 0),
                        "challenge_count": int(action_counts.get("challenge") or 0),
                        "reject_count": int(action_counts.get("reject") or 0),
                        "blockers": review_blockers,
                        "focus": focus,
                        "label": (
                            "候选当前版本均已完成风险复核"
                            if review_ready
                            else "候选精确版本风控阻止收敛"
                        ),
                        "review_actions_are_dispositions_only": True,
                        "execution_capability": "none",
                        "live_trading_allowed": False,
                        "can_autonomously_decide": False,
                    }
            except Exception:
                candidate_lineage_gate = {
                    **candidate_lineage_gate,
                    "applicable": True,
                    "version": CANDIDATE_LINEAGE_VERSION,
                    "ready": False,
                    "status": "projection_failed",
                    "blockers": [self._issue(
                        "CANDIDATE_LINEAGE_PROJECTION_FAILED",
                        "候选对象谱系无法核验",
                        "本轮候选谱系投影失败；在完成确定性复核前不能结束讨论。",
                        "candidate_lineage",
                    )],
                    "focus": {
                        "code": "CANDIDATE_LINEAGE_PROJECTION_FAILED",
                        "title": "候选对象谱系无法核验",
                        "detail": "先由决策整合角色重建可核验的候选引用。",
                        "target_capabilities": ["decision_synthesis"],
                        "target_stances": ["portfolio_manager"],
                        "repair_scope": "in_round",
                        "coverage_mode": "until_resolved",
                        "routing_priority": "candidate_repair",
                    },
                    "label": "候选对象谱系阻止收敛",
                }
                if candidate_risk_review_required:
                    candidate_risk_review_gate = {
                        **candidate_risk_review_gate,
                        "applicable": True,
                        "version": CANDIDATE_RISK_REVIEW_VERSION,
                        "ready": False,
                        "status": "projection_failed",
                        "blockers": [self._issue(
                            "CANDIDATE_RISK_REVIEW_PROJECTION_FAILED",
                            "候选风险复核无法核验",
                            "本轮候选风险复核投影失败；完成确定性复核前不能结束讨论。",
                            "candidate_risk_review",
                        )],
                        "focus": {
                            "code": "CANDIDATE_RISK_REVIEW_PROJECTION_FAILED",
                            "title": "候选风险复核无法核验",
                            "detail": "先修复候选风险复核投影，再继续决策整合。",
                            "target_capabilities": ["risk_review"],
                            "target_stances": ["risk"],
                        },
                        "label": "候选精确版本风控阻止收敛",
                    }

        if (
            candidate_risk_review_version
            and candidate_risk_review_version != CANDIDATE_RISK_REVIEW_VERSION
        ):
            candidate_risk_review_gate = {
                **candidate_risk_review_gate,
                "applicable": True,
                "version": candidate_risk_review_version,
                "ready": False,
                "status": "unsupported_version",
                "blockers": [self._issue(
                    "CANDIDATE_RISK_REVIEW_VERSION_UNSUPPORTED",
                    "候选风控协议版本不受支持",
                    "本轮冻结的候选风控协议无法由当前服务核验。",
                    "candidate_risk_review",
                )],
                "label": "候选精确版本风控阻止收敛",
            }

        discussion_blockers: list[dict[str, str]] = []
        if not active_round_id:
            discussion_blockers.append(self._issue("ROUND_NOT_STARTED", "尚未开始讨论", "先定义本轮目标并发起一轮。", "discussion"))
        if not objective_ready:
            discussion_blockers.append(self._issue("OBJECTIVE_MISSING", "本轮目标不明确", "补充需要比较、验证或产出的具体目标。", "discussion"))
        if turn_contract_required and not turn_contract_gate["ready"]:
            discussion_blockers.append(self._issue(
                "TURN_CONTRACTS_INCOMPLETE",
                "正式发言合同尚未全部核验",
                "仅合格的 turn_contract_v1 正式发言可以计入角色、阶段和反证覆盖。",
                "discussion",
            ))
        if candidate_lineage_gate["applicable"] and not candidate_lineage_gate["ready"]:
            discussion_blockers.extend(candidate_lineage_gate["blockers"])
        if (
            candidate_risk_review_gate["applicable"]
            and not candidate_risk_review_gate["ready"]
        ):
            discussion_blockers.extend(candidate_risk_review_gate["blockers"])
        if len(successful_enabled_ids) < required_success_count:
            discussion_blockers.append(self._issue(
                "SPEAKER_COVERAGE_INCOMPLETE",
                "有效发言覆盖不足",
                (
                    f"已完成 {len(successful_enabled_ids)} 位，政策要求至少 "
                    f"{required_success_count} 位不同成员成功发言；当前启用 {len(enabled_members)} 位。"
                ),
                "discussion",
            ))
        for item in stage_coverage:
            if item["configured_count"] < item["required_count"]:
                discussion_blockers.append(self._issue(
                    f"STAGE_{item['id'].upper()}_MISSING",
                    f"缺少“{item['label']}”阶段成员",
                    (
                        f"已配置 {item['configured_count']} 位，政策要求 {item['required_count']} 位；"
                        "可编辑成员 workflow_stage 或调整房间政策。"
                    ),
                    "discussion",
                ))
            elif item["successful_count"] < item["required_count"]:
                discussion_blockers.append(self._issue(
                    f"STAGE_{item['id'].upper()}_UNHEARD",
                    f"“{item['label']}”阶段尚未完成",
                    (
                        f"成功覆盖 {item['successful_count']} / {item['required_count']}。"
                        "成员改换阶段后，旧身份发言不会冒充新阶段覆盖。"
                    ),
                    "discussion",
                ))
        for item in role_coverage:
            if item["configured_count"] < item["required_count"]:
                discussion_blockers.append(self._issue(
                    f"ROLE_{item['id'].upper()}_MISSING",
                    f"缺少{item['label']}角色",
                    (
                        f"已配置 {item['configured_count']} 位，政策要求 {item['required_count']} 位；"
                        "可编辑成员 stance/capabilities 或调整房间政策。"
                    ),
                    "discussion",
                ))
            elif item["successful_count"] < item["required_count"]:
                discussion_blockers.append(self._issue(
                    f"ROLE_{item['id'].upper()}_UNHEARD",
                    f"{item['label']}尚未有效发言",
                    (
                        f"成功覆盖 {item['successful_count']} / {item['required_count']}。"
                        "模型失败、仅被调度或发言后才改成该身份都不算完成。"
                    ),
                    "discussion",
                ))

        discussion_ready = (
            objective_ready
            and count_ready
            and coverage_ready
            and bool(turn_contract_gate["ready"])
            and bool(candidate_lineage_gate["ready"])
            and bool(candidate_risk_review_gate["ready"])
        )
        artifacts = self.store.list_artifacts(room_id)
        round_artifacts = [artifact for artifact in artifacts if str(artifact.get("round_id") or "") == active_round_id]
        latest_artifact = round_artifacts[0] if round_artifacts else None
        frozen_project_workspace = (
            checkpoint_state.get("project_workspace")
            if isinstance(checkpoint_state.get("project_workspace"), dict)
            else None
        )
        use_frozen_project_workspace = bool(
            is_project
            and checkpoint_state
            and (
                str(round_id or "").strip()
                or str(latest_round.get("status") or "").upper() in {"RUNNING", "PAUSED"}
            )
        )
        if use_frozen_project_workspace and frozen_project_workspace is not None:
            project_workspace = frozen_project_workspace
        elif use_frozen_project_workspace:
            project_workspace = self.legacy_project_workspace_snapshot()
        else:
            project_workspace = self._project_workspace_snapshot(
                artifacts[0] if artifacts else None,
                applicable=is_project,
                frozen=False,
            )
        counter_requirements = [
            requirement
            for requirement in workflow_policy["required_coverage"]
            if requirement.get("is_counterargument")
        ]
        counter_role_ids = {
            str(member.get("id") or "")
            for member in enabled_members
            if any(
                member_matches_requirement(member, requirement)
                for requirement in counter_requirements
            )
        }
        counter_success_ids = {
            str(member.get("id") or "")
            for member in successful_member_versions
            if any(
                member_matches_requirement(member, requirement)
                for requirement in counter_requirements
            )
        }.intersection(counter_role_ids)
        counter_applicable = bool(counter_requirements)
        counter_message_ids = {
            str(message.get("id") or "")
            for message in round_messages
            if str(message.get("sender_id") or "") in counter_success_ids
            and str(message.get("id") or "")
        }
        evidence_gate = self._evidence_gate(
            latest_artifact,
            required_counter_message_ids=(
                counter_message_ids
                if is_storage and counter_applicable
                else None
            ),
        )
        decision_gate = self._decision_gate(
            latest_artifact,
            is_storage=is_storage,
            is_project=is_project,
        )
        user_decision_gate = self._user_decision_gate(latest_artifact)
        market_snapshot = runtime_state.get("market_snapshot")
        if not isinstance(market_snapshot, dict):
            market_snapshot = checkpoint_state.get("market_snapshot") if isinstance(checkpoint_state.get("market_snapshot"), dict) else None
        data_gate = self._data_gate(
            room_snapshot,
            round_messages,
            market_snapshot,
            is_storage,
            enforce_research_quality=True,
        )
        research_evidence_gate = data_gate["research_evidence_gate"]
        simulation_gate = self._simulation_gate(room_snapshot, is_storage)
        portfolio_gate = self._portfolio_gate(
            room_snapshot,
            is_storage,
            user_decision_gate,
        )
        workflow_configuration_gate = self.workflow_configuration_preflight(room_snapshot)

        counter_gate = {
            "applicable": counter_applicable,
            "ready": (not counter_applicable) or (bool(counter_role_ids) and bool(counter_success_ids)),
            "configured_count": len(counter_role_ids),
            "successful_count": len(counter_success_ids),
            "artifact_counter_evidence_count": evidence_gate["counter_evidence_count"],
            "artifact_disputed_evidence_count": evidence_gate["disputed_evidence_count"],
            "qualified_counter_evidence_count": evidence_gate[
                "qualified_counter_evidence_count"
            ],
            "artifact_counter_evidence_ready": evidence_gate[
                "counter_evidence_ready"
            ],
            "unresolved_disagreement_count": evidence_gate["unresolved_disagreement_count"],
            "label": (
                "政策未要求独立反证"
                if not counter_applicable
                else "反证角色与产物证据均已覆盖"
                if counter_role_ids
                and counter_success_ids
                and evidence_gate["counter_evidence_ready"]
                else "反证角色已覆盖"
                if counter_role_ids and counter_success_ids
                else "反证角色尚未覆盖"
            ),
        }

        can_host_finish = discussion_ready and data_gate["ready"]
        recommendation_ready = (
            discussion_ready
            and evidence_gate["ready"]
            and decision_gate["ready"]
            and data_gate["ready"]
        )
        user_action = str(user_decision_gate.get("action") or "")
        research_ready = bool(
            recommendation_ready
            and user_action == "support"
            and portfolio_gate["ready"]
        )
        if not active_round_id:
            decision_status = "NOT_STARTED"
            label = "尚未开始收敛检查"
        elif not discussion_ready:
            decision_status = "DISCUSSION_INCOMPLETE"
            label = "需要继续讨论"
        elif not research_evidence_gate["ready"]:
            decision_status = "RESEARCH_EVIDENCE_REPAIR_REQUIRED"
            label = "研究证据质量阻止收敛"
        elif not latest_artifact:
            decision_status = "DRAFT_REQUIRED"
            label = "可整理候选方案"
        elif not evidence_gate["ready"]:
            decision_status = "EVIDENCE_REVIEW_REQUIRED"
            label = "等待证据复核"
        elif not decision_gate["ready"]:
            decision_status = "DECISION_SLATE_REQUIRED"
            label = "等待多方案比较与首选理由"
        elif simulation_gate["pending_user_confirmation_count"]:
            decision_status = "USER_CONFIRMATION_REQUIRED"
            label = "等待用户确认模拟观察"
        elif (
            user_action == "support"
            and portfolio_gate["applicable"]
            and not portfolio_gate["ready"]
        ):
            decision_status = "PORTFOLIO_REVIEW_REQUIRED"
            label = "等待决定包的模拟组合通过风险复核"
        elif data_gate["ready"]:
            if user_action == "support":
                decision_status = "USER_SUPPORTED"
                label = "用户支持的研究方案已通过模拟风险复核"
            elif user_action == "hold":
                decision_status = "USER_HELD"
                label = "用户决定暂时保留"
            elif user_action == "return":
                decision_status = "RETURNED_FOR_REVISION"
                label = "用户已退回修订"
            else:
                decision_status = "READY_FOR_USER_DECISION"
                label = "可交由用户决策"
        else:
            decision_status = "DATA_GAP_REMAINS"
            label = "证据数据仍有缺口"

        blockers = list(discussion_blockers)
        if discussion_ready:
            blockers.extend(evidence_gate["blockers"])
            blockers.extend(decision_gate["blockers"])
            blockers.extend(data_gate["blockers"])
            blockers.extend(portfolio_gate["blockers"])
        warnings = list(data_gate["warnings"])
        warnings.extend(simulation_gate["warnings"])
        warnings.extend(portfolio_gate["warnings"])
        if evidence_gate["disputed_evidence_count"]:
            warnings.append("会议产物仍保留存在争议的证据；争议记录不会被自动抹平。")

        next_actions: list[str] = []
        if discussion_blockers:
            next_actions.append(discussion_blockers[0]["detail"])
        elif not research_evidence_gate["ready"]:
            next_actions.append(research_evidence_gate["blockers"][0]["detail"])
        elif not latest_artifact:
            next_actions.append("生成本轮会议纪要草稿，并逐条绑定支持、反证和背景证据。")
        elif not evidence_gate["ready"]:
            next_actions.append("由用户核对产物中的证据用途、来源状态和版本变化后再确认。")
        elif not decision_gate["ready"]:
            next_actions.append("至少比较两个候选方案，选择一个首选方案并记录可核验的选择理由。")
        elif simulation_gate["pending_user_confirmation_count"]:
            next_actions.append("由用户决定是否确认待处理的模拟观察；确认前不会冻结基准价。")
        elif (
            user_action == "support"
            and portfolio_gate["applicable"]
            and not portfolio_gate["ready"]
        ):
            next_actions.append("从当前支持决定建立模拟组合，调整权重或风险预算，并由用户确认精确版本。")
        elif user_decision_gate.get("action") == "support":
            next_actions.append("用户已支持当前候选；继续保留证据版本和失效条件，系统不会执行任何资金动作。")
        elif user_decision_gate.get("action") == "hold":
            next_actions.append("用户选择暂时保留；按决定理由补充证据或等待条件变化后再判断。")
        elif user_decision_gate.get("action") == "return":
            next_actions.append("用户已退回当前候选；修改产物后需重新完成证据确认和最终决定。")
        else:
            next_actions.append("用户可支持、保留或退回候选方案；系统不会代替用户做最终决定。")
        if is_storage and not simulation_gate["statistical_claim_allowed"]:
            next_actions.append(f"累计至少 {OBSERVATION_MIN_SAMPLES} 个用户确认且已到期样本后，才显示统计胜率。")

        return {
            "version": 2,
            "room_id": room_id,
            "round_id": active_round_id,
            "template_id": str(room.get("template_id") or "open_collaboration"),
            "decision_status": decision_status,
            "label": label,
            "can_host_finish": can_host_finish,
            "can_present_candidate_best": recommendation_ready,
            "research_ready": research_ready,
            "can_autonomously_decide": False,
            "user_confirmation_required": not bool(user_decision_gate.get("ready")),
            "workflow_policy": workflow_policy,
            "workflow_configuration_gate": workflow_configuration_gate,
            "turn_contract_gate": turn_contract_gate,
            "candidate_lineage_gate": candidate_lineage_gate,
            "candidate_risk_review_gate": candidate_risk_review_gate,
            "discussion_gate": {
                "ready": discussion_ready,
                "successful_member_count": len(successful_enabled_ids),
                "required_success_count": required_success_count,
                "stage_coverage": stage_coverage,
                "role_coverage": role_coverage,
                "blockers": discussion_blockers,
                "label": "讨论覆盖完成" if discussion_ready else "讨论覆盖未完成",
            },
            "counterargument_gate": counter_gate,
            "research_evidence_gate": research_evidence_gate,
            "project_workspace": project_workspace,
            "evidence_gate": evidence_gate,
            "decision_gate": decision_gate,
            "user_decision_gate": user_decision_gate,
            "data_gate": data_gate,
            "simulation_gate": simulation_gate,
            "portfolio_gate": portfolio_gate,
            "blockers": blockers,
            "warnings": warnings,
            "next_actions": next_actions,
            "execution_capability": "none",
            "live_trading_allowed": False,
            "boundary": "只能形成候选研究方案、回测或模拟观察；最终决定属于用户，禁止真实下单。",
        }

    @staticmethod
    def _role_coverage(
        members: list[dict[str, Any]],
        successful_members: list[dict[str, Any]],
        requirements: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        coverage: list[dict[str, Any]] = []
        for requirement in requirements:
            configured = [
                member
                for member in members
                if member_matches_requirement(member, requirement)
            ]
            configured_ids = {str(member.get("id") or "") for member in configured}
            successful = {
                str(member.get("id") or "")
                for member in successful_members
                if str(member.get("id") or "") in configured_ids
                and member_matches_requirement(member, requirement)
            }
            selectors = requirement.get("any_of") or {}
            required_count = int(requirement.get("minimum") or 1)
            coverage.append({
                "id": str(requirement.get("id") or ""),
                "label": str(requirement.get("label") or ""),
                "stances": list(selectors.get("stances") or []),
                "capabilities": list(selectors.get("capabilities") or []),
                "is_counterargument": bool(requirement.get("is_counterargument")),
                "required_count": required_count,
                "configured_count": len(configured),
                "successful_count": len(successful),
                "configured_member_ids": [str(member.get("id") or "") for member in configured],
                "successful_member_ids": sorted(successful),
                "ready": len(configured) >= required_count and len(successful) >= required_count,
            })
        return coverage

    @staticmethod
    def _stage_coverage(
        members: list[dict[str, Any]],
        successful_members: list[dict[str, Any]],
        requirements: dict[str, int],
    ) -> list[dict[str, Any]]:
        coverage: list[dict[str, Any]] = []
        for stage, required_count in requirements.items():
            configured = [
                member
                for member in members
                if str(member.get("workflow_stage") or "flexible") == stage
            ]
            configured_ids = {str(member.get("id") or "") for member in configured}
            successful = {
                str(member.get("id") or "")
                for member in successful_members
                if str(member.get("id") or "") in configured_ids
                and str(member.get("workflow_stage") or "flexible") == stage
            }
            coverage.append({
                "id": stage,
                "label": stage,
                "required_count": int(required_count),
                "configured_count": len(configured),
                "successful_count": len(successful),
                "configured_member_ids": [str(member.get("id") or "") for member in configured],
                "successful_member_ids": sorted(successful),
                "ready": len(configured) >= int(required_count) and len(successful) >= int(required_count),
            })
        return coverage

    @staticmethod
    def _artifact_evidence(
        artifact: dict[str, Any] | None,
    ) -> tuple[list[dict[str, Any]], int, int, int, int]:
        if not artifact:
            return [], 0, 0, 0, 0
        content = artifact.get("content") if isinstance(artifact.get("content"), dict) else {}
        refs = [ref for ref in content.get("summary_evidence") or [] if isinstance(ref, dict)]
        disagreements = content.get("disagreements") if isinstance(content.get("disagreements"), list) else []
        unresolved_blocking = sum(
            1
            for item in disagreements
            if isinstance(item, dict)
            and item.get("blocking") is not False
            and str(item.get("status") or "open") == "open"
        )
        risks = content.get("risks") if isinstance(content.get("risks"), list) else []
        unresolved_blocking_risks = sum(
            1
            for item in risks
            if isinstance(item, dict)
            and item.get("blocking") is not False
            and str(item.get("status") or "open") in {"open", "monitoring"}
        )
        for section in ARTIFACT_SECTIONS:
            for item in content.get(section) or []:
                if isinstance(item, dict):
                    refs.extend(ref for ref in item.get("evidence") or [] if isinstance(ref, dict))
        decision = content.get("decision") if isinstance(content.get("decision"), dict) else {}
        refs.extend(ref for ref in decision.get("evidence") or [] if isinstance(ref, dict))
        for option in decision.get("options") or []:
            if isinstance(option, dict):
                refs.extend(ref for ref in option.get("evidence") or [] if isinstance(ref, dict))
        return refs, len(disagreements), unresolved_blocking, len(risks), unresolved_blocking_risks

    def _evidence_gate(
        self,
        artifact: dict[str, Any] | None,
        *,
        required_counter_message_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        (
            refs,
            disagreement_count,
            unresolved_disagreement_count,
            risk_count,
            unresolved_risk_count,
        ) = self._artifact_evidence(artifact)
        unreviewed = sum(1 for ref in refs if str(ref.get("verification_status") or "unreviewed") == "unreviewed")
        disputed = sum(1 for ref in refs if str(ref.get("verification_status") or "") == "disputed")
        counter = sum(1 for ref in refs if str(ref.get("evidence_role") or "") == "counter")
        counter_required = required_counter_message_ids is not None
        required_counter_ids = required_counter_message_ids or set()
        qualified_counter_evidence_count = sum(
            1
            for ref in refs
            if str(ref.get("type") or "") == "message"
            and str(ref.get("id") or "") in required_counter_ids
            and (
                str(ref.get("evidence_role") or "") == "counter"
                or str(ref.get("verification_status") or "") == "disputed"
            )
            and str(ref.get("verification_status") or "unreviewed") != "unreviewed"
        )
        counter_evidence_ready = (
            not counter_required or qualified_counter_evidence_count > 0
        )
        stale = sum(
            1 for ref in refs
            if str(ref.get("version_status") or "current") != "current"
            and str(ref.get("version_decision") or "review_required") != "keep_snapshot"
        )
        blockers: list[dict[str, str]] = []
        if not artifact:
            blockers.append(self._issue("ARTIFACT_MISSING", "尚未形成会议产物", "先把讨论整理成可逐条审查的会议纪要草稿。", "evidence"))
        elif str(artifact.get("status") or "DRAFT").upper() != "CONFIRMED":
            blockers.append(self._issue("ARTIFACT_UNCONFIRMED", "会议产物尚未确认", "用户需要逐条检查证据后确认产物。", "evidence"))
        if unreviewed:
            blockers.append(self._issue("EVIDENCE_UNREVIEWED", "仍有未核验证据", f"共有 {unreviewed} 条证据关系尚未核对原文或交叉来源。", "evidence"))
        if stale:
            blockers.append(self._issue("EVIDENCE_VERSION_DRIFT", "证据版本发生变化", f"共有 {stale} 条证据需要迁移到当前版本或说明为何保留历史快照。", "evidence"))
        if unresolved_disagreement_count:
            blockers.append(self._issue(
                "DISAGREEMENT_OPEN",
                "仍有阻断性分歧未处理",
                (
                    f"共有 {unresolved_disagreement_count} 条分歧仍标记为 open；"
                    "需要解决，或由用户明确记录为何接受该风险。"
                ),
                "evidence",
            ))
        if unresolved_risk_count:
            blockers.append(self._issue(
                "PROJECT_RISK_OPEN",
                "仍有阻断性项目风险未处理",
                (
                    f"共有 {unresolved_risk_count} 条风险仍处于 open 或 monitoring；"
                    "需要完成缓解，或由用户明确记录接受风险并取消阻断。"
                ),
                "evidence",
            ))
        if not counter_evidence_ready:
            blockers.append(self._issue(
                "COUNTER_EVIDENCE_MISSING",
                "反证未进入最终产物",
                (
                    "至少保留一条来自本轮合格空头或风控正式发言的已复核反证关系；"
                    "不能只记录反证角色发过言。"
                ),
                "evidence",
            ))
        ready = (
            bool(artifact)
            and str(artifact.get("status") or "").upper() == "CONFIRMED"
            and not unreviewed
            and not stale
            and not unresolved_disagreement_count
            and not unresolved_risk_count
            and counter_evidence_ready
        )
        return {
            "ready": ready,
            "status": "current_confirmed" if ready else "review_required" if artifact else "not_created",
            "artifact_id": str((artifact or {}).get("id") or ""),
            "artifact_version": int((artifact or {}).get("version") or 0),
            "artifact_status": str((artifact or {}).get("status") or "NONE").upper(),
            "evidence_count": len(refs),
            "unreviewed_evidence_count": unreviewed,
            "disputed_evidence_count": disputed,
            "counter_evidence_count": counter,
            "qualified_counter_evidence_count": qualified_counter_evidence_count,
            "counter_evidence_required": counter_required,
            "counter_evidence_ready": counter_evidence_ready,
            "stale_evidence_count": stale,
            "disagreement_count": disagreement_count,
            "unresolved_disagreement_count": unresolved_disagreement_count,
            "risk_count": risk_count,
            "unresolved_risk_count": unresolved_risk_count,
            "blockers": blockers,
            "label": "证据已由用户复核" if ready else "证据等待用户复核",
        }

    def _decision_gate(
        self,
        artifact: dict[str, Any] | None,
        *,
        is_storage: bool,
        is_project: bool = False,
    ) -> dict[str, Any]:
        if not (is_storage or is_project):
            return {
                "applicable": False,
                "ready": True,
                "status": "not_required",
                "option_count": 0,
                "preferred_option_id": "",
                "blockers": [],
                "label": "当前房间不强制多方案决策板",
            }
        content = artifact.get("content") if isinstance(artifact, dict) and isinstance(artifact.get("content"), dict) else {}
        decision = content.get("decision") if isinstance(content.get("decision"), dict) else {}
        options = [item for item in decision.get("options") or [] if isinstance(item, dict)]
        option_ids = {str(item.get("id") or "") for item in options if str(item.get("id") or "")}
        preferred_option_id = str(decision.get("preferred_option_id") or "")
        status = str(decision.get("status") or "undecided")
        rationale_ready = bool(str(decision.get("rationale") or "").strip())
        blockers: list[dict[str, str]] = []
        context_label = "存储产业投委会" if is_storage else "结构化项目研究"
        if len(options) < 2:
            blockers.append(self._issue(
                "DECISION_OPTIONS_INSUFFICIENT",
                "候选方案不足",
                f"{context_label}必须保存至少两个使用共同维度比较的候选方案。",
                "decision",
            ))
        if status != "candidate" or preferred_option_id not in option_ids:
            blockers.append(self._issue(
                "PREFERRED_OPTION_MISSING",
                "尚未选择首选方案",
                "从候选方案中选择一个首选项；它仍需用户最终确认，不能自动执行。",
                "decision",
            ))
        if not rationale_ready:
            blockers.append(self._issue(
                "DECISION_RATIONALE_MISSING",
                "首选理由缺失",
                "记录选择依据、主要反证和放弃其他方案的原因。",
                "decision",
            ))
        ready = not blockers
        return {
            "applicable": True,
            "ready": ready,
            "status": "candidate_selected" if ready else "comparison_required",
            "option_count": len(options),
            "preferred_option_id": preferred_option_id,
            "rationale_ready": rationale_ready,
            "blockers": blockers,
            "label": "多方案比较完成" if ready else "等待多方案比较",
        }

    @staticmethod
    def _user_decision_gate(
        artifact: dict[str, Any] | None,
    ) -> dict[str, Any]:
        decision = (
            artifact.get("user_decision")
            if isinstance(artifact, dict)
            and isinstance(artifact.get("user_decision"), dict)
            else None
        )
        history = (
            artifact.get("user_decision_history")
            if isinstance(artifact, dict)
            and isinstance(artifact.get("user_decision_history"), list)
            else []
        )
        governance = (
            artifact.get("governance_snapshot")
            if isinstance(artifact, dict)
            and isinstance(artifact.get("governance_snapshot"), dict)
            else {}
        )
        governance_state = (
            governance.get("user_decision_state")
            if isinstance(governance.get("user_decision_state"), dict)
            else {}
        )
        governance_applicable = governance.get("applicable") is True
        governance_decision_id = str(governance_state.get("decision_id") or "")
        current = (
            decision
            if decision
            and decision.get("is_current") is True
            and decision.get("integrity_ok") is True
            and decision.get("candidate_binding_integrity_ok") is True
            and (
                not governance_applicable
                or (
                    governance.get("integrity_ok") is True
                    and governance_state.get("is_current") is True
                    and str(decision.get("id") or "")
                    == governance_decision_id
                    and str(
                        decision.get("selected_option_id") or ""
                    ) == str(
                        governance_state.get("selected_option_id") or ""
                    )
                )
            )
            else None
        )
        action = str((current or {}).get("action") or "")
        labels = {
            "support": "用户已支持候选方案",
            "hold": "用户决定暂时保留",
            "return": "用户已退回修订",
        }
        return {
            "applicable": bool(artifact),
            "ready": bool(current),
            "status": (
                "user_supported"
                if action == "support"
                else "user_held"
                if action == "hold"
                else "returned_for_revision"
                if action == "return"
                else "awaiting_user_decision"
                if artifact
                else "artifact_required"
            ),
            "action": action,
            "decision_id": str((current or {}).get("id") or ""),
            "artifact_id": str((artifact or {}).get("id") or ""),
            "artifact_version": int((artifact or {}).get("version") or 0),
            "decision_version": str((current or {}).get("decision_version") or ""),
            "ai_preferred_option_id": str(
                (current or {}).get("ai_preferred_option_id") or ""
            ),
            "selected_option_id": str(
                (current or {}).get("selected_option_id") or ""
            ),
            "preferred_option_id": str(
                (current or {}).get("selected_option_id") or ""
            ),
            "selected_option_revision": int(
                (current or {}).get("selected_option_revision") or 0
            ),
            "selected_option_origin_message_id": str(
                (current or {}).get("selected_option_origin_message_id") or ""
            ),
            "selected_option_latest_message_id": str(
                (current or {}).get("selected_option_latest_message_id") or ""
            ),
            "selected_option_snapshot_sha256": str(
                (current or {}).get("selected_option_snapshot_sha256") or ""
            ),
            "selected_option_risk_review_required": bool(
                (current or {}).get("selected_option_risk_review_required")
            ),
            "selected_is_ai_preferred": bool(
                (current or {}).get("selected_is_ai_preferred")
            ),
            "history_count": len(history),
            "label": labels.get(action, "等待用户支持、保留或退回候选方案"),
            "can_autonomously_decide": False,
            "execution_capability": "none",
            "live_trading_allowed": False,
        }

    def _data_gate(
        self,
        snapshot: dict[str, Any],
        round_messages: list[dict[str, Any]],
        market_snapshot: dict[str, Any] | None,
        is_storage: bool,
        *,
        enforce_research_quality: bool = False,
    ) -> dict[str, Any]:
        materials = snapshot.get("materials") or []
        cited_material_ids = {
            str(citation.get("id") or "")
            for message in round_messages
            for citation in message.get("citations") or []
            if str(citation.get("id") or "")
        }
        official_materials = [
            material for material in materials
            if str((material.get("metadata") or {}).get("official_evidence_kind") or "") in {"sec_filing", "ir_release"}
        ]
        blockers: list[dict[str, str]] = []
        warnings: list[str] = []
        snapshot_payload = market_snapshot if isinstance(market_snapshot, dict) else {}
        quote_validation = validate_storage_quote_snapshot(snapshot_payload)
        market_symbols = set(quote_validation["market_symbols"])
        ready_symbols = set(quote_validation["ready_symbols"])
        invalid_market_time_symbols = set(
            quote_validation["invalid_market_time_symbols"]
        )
        future_market_time_symbols = set(
            quote_validation["future_market_time_symbols"]
        )
        invalid_freshness_symbols = set(
            quote_validation["invalid_freshness_symbols"]
        )
        safety_fields_explicit = quote_validation["safety_fields_explicit"]
        safe_snapshot = not snapshot_payload or quote_validation["safe_snapshot"]
        snapshot_quality_ready = quote_validation["snapshot_quality_ready"]
        market_complete = snapshot_quality_ready
        if is_storage and invalid_market_time_symbols:
            blockers.append(self._issue(
                "MARKET_SNAPSHOT_TIME_INVALID",
                "行情时间无法与冻结时间核验",
                "无法解析 " + ", ".join(sorted(invalid_market_time_symbols)) + " 的行情时间；必须重新冻结，不得把未知时间数据标为 ready。",
                "data",
            ))
        if is_storage and future_market_time_symbols:
            blockers.append(self._issue(
                "MARKET_SNAPSHOT_TIME_FUTURE",
                "行情时间晚于冻结时间",
                ", ".join(sorted(future_market_time_symbols)) + " 的行情时间晚于 captured_at；存在时钟错误或未来数据泄漏风险。",
                "data",
            ))
        if is_storage and invalid_freshness_symbols:
            blockers.append(self._issue(
                "MARKET_SNAPSHOT_FRESHNESS_INVALID",
                "行情新鲜度合同不完整或不一致",
                ", ".join(sorted(invalid_freshness_symbols)) + " 必须明确属于 20 分钟实时窗，或 96 小时内且市场状态明确闭市的非实时截面；旧版缺字段记录不准入。",
                "data",
            ))
        if is_storage and not market_complete:
            blockers.append(self._issue(
                "MARKET_SNAPSHOT_INCOMPLETE",
                "统一行情截面不完整或质量不足",
                "必须使用同轮 Futu OpenD ready 快照；四行都需有效价格、市场时间和显式新鲜度合同：20 分钟实时窗，或 96 小时内的明确闭市截面。",
                "data",
            ))
        if is_storage and not official_materials:
            warnings.append("尚未冻结 SEC 或公司 IR 一级来源；可讨论，但不能把二手叙事升级为已核实事实。")
        if materials and not cited_material_ids:
            warnings.append("本轮已有共享资料，但成功发言中尚未形成可审计的资料引用。")
        if not safe_snapshot:
            blockers.append(self._issue("EXECUTION_BOUNDARY_BROKEN", "数据源越过只读边界", "停止收敛并恢复 execution_capability=none。", "safety"))
        room_payload = snapshot.get("room") if isinstance(snapshot.get("room"), dict) else {}
        expected_room_id = str(room_payload.get("id") or "").strip()
        research_evidence_gate = (
            self._storage_research_evidence_gate(
                snapshot_payload,
                expected_room_id=expected_room_id,
            )
            if is_storage and enforce_research_quality
            else {
                "applicable": bool(is_storage),
                "evaluated": False,
                "ready": True,
                "state": "admission_quote_only" if is_storage else "not_applicable",
                "blockers": [],
                "warnings": [],
                "focus": None,
                "repair_scope": "none",
                "label": (
                    "轮次准入仅检查统一行情与只读边界"
                    if is_storage
                    else "当前房间不使用存储研究证据门"
                ),
            }
        )
        if enforce_research_quality:
            blockers.extend(research_evidence_gate["blockers"])
            warnings.extend(research_evidence_gate["warnings"])
        ready = (
            safe_snapshot
            and (market_complete if is_storage else True)
            and research_evidence_gate["ready"]
        )
        return {
            "ready": ready,
            "active_material_count": len(materials),
            "cited_material_count": len(cited_material_ids),
            "official_material_count": len(official_materials),
            "market_snapshot_required": is_storage,
            "market_snapshot_complete": market_complete if is_storage else None,
            "market_symbols": sorted(market_symbols),
            "ready_market_symbols": sorted(ready_symbols),
            "invalid_market_time_symbols": sorted(invalid_market_time_symbols),
            "future_market_time_symbols": sorted(future_market_time_symbols),
            "invalid_freshness_symbols": sorted(invalid_freshness_symbols),
            "snapshot_source": str(snapshot_payload.get("source") or ""),
            "snapshot_state": str(snapshot_payload.get("state") or ""),
            "snapshot_quality_ready": snapshot_quality_ready if is_storage else None,
            "safety_fields_explicit": safety_fields_explicit if snapshot_payload else None,
            "snapshot_id": str((market_snapshot or {}).get("snapshot_id") or ""),
            "research_evidence_gate": research_evidence_gate,
            "blockers": blockers,
            "warnings": warnings,
            "label": "统一数据截面可用" if ready else "统一数据截面仍有缺口",
        }

    @staticmethod
    def _parse_snapshot_time(value: Any) -> datetime | None:
        text = str(value or "").strip()
        if not text:
            return None
        candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _storage_research_evidence_gate(
        self,
        market_snapshot: dict[str, Any],
        *,
        expected_room_id: str = "",
    ) -> dict[str, Any]:
        """Fail closed on severe defects in a frozen storage research bundle.

        The gate consumes only the already-frozen snapshot. It performs no network,
        provider, account, or execution action, and therefore remains deterministic
        across pause/resume and audit replay.
        """

        blockers: list[dict[str, Any]] = []
        warnings: list[str] = []

        def add_blocker(
            code: str,
            title: str,
            detail: str,
            *,
            capabilities: list[str],
            stances: list[str],
        ) -> None:
            issue: dict[str, Any] = self._issue(
                code,
                title,
                detail,
                "research_evidence",
            )
            issue["target_capabilities"] = capabilities
            issue["target_stances"] = stances
            # This gate evaluates the immutable round snapshot.  A visible
            # turn can explain or reject a defect, but cannot mutate the
            # already-frozen evidence bundle and must never claim it repaired.
            issue["repair_scope"] = "next_round_only"
            blockers.append(issue)

        evidence = (
            market_snapshot.get("evidence")
            if isinstance(market_snapshot.get("evidence"), dict)
            else None
        )
        if evidence is None:
            add_blocker(
                "STORAGE_RESEARCH_EVIDENCE_MISSING",
                "存储研究证据包缺失",
                "冻结快照只有行情外壳，没有可审计的技术指标与官方来源；由数据质量官停止引用，下一新轮重新建立完整证据包。",
                capabilities=["data_quality_review"],
                stances=["data_guardian"],
            )
            return {
                "applicable": True,
                "evaluated": True,
                "ready": False,
                "state": "missing",
                "technical_max_age_days": STORAGE_TECHNICAL_MAX_AGE_DAYS,
                "blockers": blockers,
                "warnings": warnings,
                "focus": blockers[0],
                "repair_scope": "next_round_only",
                "label": "存储研究证据包缺失",
                "execution_capability": "none",
                "live_trading_allowed": False,
            }

        evidence_state = str(evidence.get("state") or "").strip().lower()
        manual_evidence = (
            evidence.get("manual_official_evidence")
            if isinstance(evidence.get("manual_official_evidence"), dict)
            else {}
        )
        manual_evidence_valid = (
            validate_manual_official_evidence(
                manual_evidence,
                expected_room_id=expected_room_id,
            )
            if manual_evidence
            else True
        )

        def contains_source_errors(value: Any) -> bool:
            return bool(effective_source_errors(
                value,
                manual_evidence,
                expected_room_id=expected_room_id,
            ))

        has_any_source_errors = contains_source_errors(evidence)
        invalid_manual_attestations = manual_evidence.get("invalid_confirmed_attestations") or []
        manual_state_claimed = trusted_manual_substitution_claimed(market_snapshot)
        manual_state_contract_invalid = bool(
            manual_state_claimed
            and (
                not manual_evidence
                or not manual_evidence_valid
                or not manual_evidence.get("source_issue_resolutions")
            )
        )
        if invalid_manual_attestations or not manual_evidence_valid or manual_state_contract_invalid:
            add_blocker(
                "STORAGE_MANUAL_OFFICIAL_EVIDENCE_INVALID",
                "人工确认的官方材料副本已失效",
                "已确认副本的资料版本或服务端哈希完整性不再成立；保留上游错误并重新上传、预览和显式确认。",
                capabilities=["data_quality_review", "fundamental_analysis"],
                stances=["data_guardian", "fundamental"],
            )
        if manual_evidence_valid and manual_evidence.get("source_issue_resolutions"):
            warnings.append("本轮含用户显式确认的精确官方材料副本；上游访问错误仍保留在审计记录中。")
        captured_at = self._parse_snapshot_time(market_snapshot.get("captured_at"))
        technical = evidence.get("technical") if isinstance(evidence.get("technical"), dict) else {}
        technical_rows = [
            row for row in technical.get("rows") or [] if isinstance(row, dict)
        ]
        technical_by_symbol = {
            str(row.get("symbol") or "").strip().upper(): row
            for row in technical_rows
            if str(row.get("symbol") or "").strip().upper() in STORAGE_SYMBOLS
        }
        missing_technical = sorted(STORAGE_SYMBOLS - set(technical_by_symbol))
        if missing_technical:
            add_blocker(
                "STORAGE_TECHNICAL_EVIDENCE_MISSING",
                "技术证据覆盖不完整",
                "缺少 " + ", ".join(missing_technical) + " 的冻结技术指标；由数据质量官标记缺失，技术分析师不得补造或沿用旧指标。",
                capabilities=["data_quality_review", "technical_analysis"],
                stances=["data_guardian", "technical"],
            )

        invalid_dates: list[str] = []
        stale_rows: list[tuple[str, int]] = []
        future_rows: list[tuple[str, int]] = []
        for symbol, row in sorted(technical_by_symbol.items()):
            as_of = self._parse_snapshot_time(row.get("as_of"))
            if captured_at is None or as_of is None:
                invalid_dates.append(symbol)
                continue
            age_days = (captured_at.date() - as_of.date()).days
            if age_days < -1:
                future_rows.append((symbol, abs(age_days)))
            elif age_days > STORAGE_TECHNICAL_MAX_AGE_DAYS:
                stale_rows.append((symbol, age_days))
        if invalid_dates:
            add_blocker(
                "STORAGE_TECHNICAL_TIME_INVALID",
                "技术证据时间无法核验",
                "无法把 " + ", ".join(invalid_dates) + " 的 technical.as_of 与冻结行情时间比较；数据质量官需拒绝相关趋势判断。",
                capabilities=["data_quality_review", "technical_analysis"],
                stances=["data_guardian", "technical"],
            )
        if stale_rows:
            detail = "、".join(f"{symbol} 过期 {days} 天" for symbol, days in stale_rows)
            add_blocker(
                "STORAGE_TECHNICAL_EVIDENCE_STALE",
                "技术指标相对冻结行情严重过期",
                f"{detail}，超过 {STORAGE_TECHNICAL_MAX_AGE_DAYS} 天硬上限；数据质量官应撤销相关技术结论，技术分析师只能在下一新轮使用重新冻结的已完成日线复算。",
                capabilities=["data_quality_review", "technical_analysis"],
                stances=["data_guardian", "technical"],
            )
        if future_rows:
            detail = "、".join(f"{symbol} 超前 {days} 天" for symbol, days in future_rows)
            add_blocker(
                "STORAGE_TECHNICAL_EVIDENCE_FUTURE",
                "技术证据晚于冻结行情",
                f"{detail}，存在未来数据泄漏风险；数据质量官必须拒绝该证据并由技术分析师重新复算。",
                capabilities=["data_quality_review", "technical_analysis"],
                stances=["data_guardian", "technical"],
            )
        if contains_source_errors(technical):
            add_blocker(
                "STORAGE_TECHNICAL_SOURCE_ERROR",
                "技术数据源明确报错",
                "冻结技术证据包含来源错误；数据质量官需保留错误并阻止责任分析师把缺失内容写成事实。",
                capabilities=["data_quality_review", "technical_analysis"],
                stances=["data_guardian", "technical"],
            )

        official_contracts = (
            (
                "official_filings",
                "SEC 官方申报",
                "filings",
                ["data_quality_review", "fundamental_analysis"],
                ["data_guardian", "fundamental"],
            ),
            (
                "company_ir_releases",
                "公司官方 IR",
                "releases",
                ["data_quality_review", "sentiment_analysis", "fundamental_analysis"],
                ["data_guardian", "sentiment", "fundamental"],
            ),
        )
        for key, label, collection_key, capabilities, stances in official_contracts:
            source = evidence.get(key) if isinstance(evidence.get(key), dict) else None
            rows = [row for row in (source or {}).get("rows") or [] if isinstance(row, dict)]
            covered = {
                str(row.get("symbol") or "").strip().upper()
                for row in rows
                if str(row.get("symbol") or "").strip().upper() in STORAGE_SYMBOLS
                and isinstance(row.get(collection_key), list)
                and bool(row.get(collection_key))
            }
            if source is None or STORAGE_SYMBOLS - covered:
                missing = sorted(STORAGE_SYMBOLS - covered)
                add_blocker(
                    f"STORAGE_{key.upper()}_MISSING",
                    f"{label}覆盖不完整",
                    f"{label}缺少 {', '.join(missing) if missing else '固定四股'} 的冻结索引；由数据质量官标记来源缺口，责任分析师不得把二手叙事升级为已核实事实。",
                    capabilities=capabilities,
                    stances=stances,
                )
            if source is not None and contains_source_errors(source):
                add_blocker(
                    f"STORAGE_{key.upper()}_SOURCE_ERROR",
                    f"{label}明确报错",
                    f"{label}在冻结时返回来源错误；保留错误原貌，修复本机只读来源配置后只能在下一新轮重新冻结。",
                    capabilities=capabilities,
                    stances=stances,
                )

        earnings_packs = (
            evidence.get("official_earnings_packs")
            if isinstance(evidence.get("official_earnings_packs"), dict)
            else None
        )
        earnings_pack_covered = covered_official_earnings_pack_symbols(
            earnings_packs,
            STORAGE_SYMBOLS,
        )
        missing_earnings_packs = sorted(STORAGE_SYMBOLS - earnings_pack_covered)
        earnings_pack_has_source_errors = bool(
            earnings_packs is not None and contains_source_errors(earnings_packs)
        )
        if earnings_packs is None or missing_earnings_packs:
            add_blocker(
                "STORAGE_OFFICIAL_EARNINGS_PACKS_MISSING",
                "官方业绩材料包覆盖不完整",
                "官方业绩材料包缺少 "
                + (", ".join(missing_earnings_packs) if missing_earnings_packs else "固定四股")
                + " 的非空冻结索引；基本面分析师不得用公司叙事替代缺失季度材料。",
                capabilities=["data_quality_review", "fundamental_analysis"],
                stances=["data_guardian", "fundamental"],
            )
        elif earnings_pack_has_source_errors:
            add_blocker(
                "STORAGE_OFFICIAL_EARNINGS_PACKS_SOURCE_ERROR",
                "官方业绩材料包明确报错",
                "官方业绩材料包或其嵌套输入包含来源错误；保留错误原貌，责任分析师不得把受影响结果写成已核实事实。",
                capabilities=["data_quality_review", "fundamental_analysis"],
                stances=["data_guardian", "fundamental"],
            )
        elif earnings_packs is not None and str(
            earnings_packs.get("state") or ""
        ).strip().lower() not in {"ready", MANUAL_SUBSTITUTION_STATE}:
            add_blocker(
                "STORAGE_OFFICIAL_EARNINGS_PACKS_NOT_READY",
                "官方业绩材料包尚未就绪",
                "official_earnings_packs.state 必须为 ready 或经完整性校验的 "
                f"{MANUAL_SUBSTITUTION_STATE}，当前为 "
                f"{str(earnings_packs.get('state') or 'missing')}。",
                capabilities=["data_quality_review", "fundamental_analysis"],
                stances=["data_guardian", "fundamental"],
            )
        supplemental_contracts = (
            ("capital_flow", "资金流证据", ["data_quality_review", "sentiment_analysis"], ["data_guardian", "sentiment"]),
            ("financial_statements", "财务报表证据", ["data_quality_review", "fundamental_analysis"], ["data_guardian", "fundamental"]),
            ("revenue_breakdown", "主营构成证据", ["data_quality_review", "fundamental_analysis"], ["data_guardian", "fundamental"]),
            ("official_earnings_materials", "官方业绩材料", ["data_quality_review", "fundamental_analysis"], ["data_guardian", "fundamental"]),
            ("industry_supply_demand", "产业供需代理", ["data_quality_review", "storage_sector_analysis"], ["data_guardian", "sector"]),
            ("research_analytics", "研究统计分析", ["data_quality_review", "risk_review", "technical_analysis"], ["data_guardian", "risk", "technical"]),
        )
        for key, label, capabilities, stances in supplemental_contracts:
            source = evidence.get(key)
            if contains_source_errors(source):
                add_blocker(
                    f"STORAGE_{key.upper()}_SOURCE_ERROR",
                    f"{label}明确报错",
                    f"{label}或其嵌套输入包含来源错误；保留错误原貌，责任分析师不得把受影响结果写成已核实事实。",
                    capabilities=capabilities,
                    stances=stances,
                )

        if has_any_source_errors:
            add_blocker(
                "STORAGE_RESEARCH_SOURCE_ERROR",
                "存储研究证据包含来源错误",
                "冻结 evidence 任一层或嵌套输入包含 source_errors；即使外层 state 被标成 ready，也必须失败关闭并保留错误原貌。",
                capabilities=["data_quality_review"],
                stances=["data_guardian"],
            )

        if evidence_state not in {"ready", MANUAL_SUBSTITUTION_STATE}:
            add_blocker(
                "STORAGE_RESEARCH_EVIDENCE_DEGRADED",
                "存储研究证据包处于降级状态",
                f"冻结 evidence.state={evidence_state or 'missing'}；数据质量官需说明降级项，相关分析师撤回受影响主张，下一新轮重新冻结后才能收敛。",
                capabilities=["data_quality_review"],
                stances=["data_guardian"],
            )

        return {
            "applicable": True,
            "evaluated": True,
            "ready": not blockers,
            "state": evidence_state or "missing",
            "technical_max_age_days": STORAGE_TECHNICAL_MAX_AGE_DAYS,
            "blockers": blockers,
            "warnings": warnings,
            "focus": blockers[0] if blockers else None,
            "repair_scope": "next_round_only" if blockers else "none",
            "label": (
                "存储研究证据质量可用于收敛"
                if not blockers
                else f"存储研究证据存在 {len(blockers)} 项阻断问题"
            ),
            "execution_capability": "none",
            "live_trading_allowed": False,
        }

    @staticmethod
    def _simulation_gate(snapshot: dict[str, Any], is_storage: bool) -> dict[str, Any]:
        if not is_storage:
            return {
                "applicable": False,
                "status": "not_applicable",
                "sample_count": 0,
                "minimum_samples": OBSERVATION_MIN_SAMPLES,
                "statistical_claim_allowed": False,
                "pending_user_confirmation_count": 0,
                "warnings": [],
                "label": "当前房间不使用交易胜率门槛",
            }
        observations = snapshot.get("observations") or []
        scorecard = snapshot.get("observation_scorecard") or {}
        scorecard_version = str(scorecard.get("version") or "")
        scorecard_contract_ready = scorecard_version == OBSERVATION_SCORECARD_VERSION
        overall = (scorecard.get("overall") or {}) if scorecard_contract_ready else {}
        sample_count = int(overall.get("sample_count") or 0)
        qualified = bool(overall.get("qualified")) and sample_count >= OBSERVATION_MIN_SAMPLES
        pending = sum(1 for observation in observations if str(observation.get("status") or "") == "PROPOSED")
        if not scorecard_contract_ready:
            status = "scorecard_version_unsupported"
            label = "统计记分卡版本未通过校验"
            warnings = [
                f"只有 {OBSERVATION_SCORECARD_VERSION} 的已核验决策谱系与 QFQ 同口径样本才能进入统计门。"
            ]
        elif qualified:
            status = "qualified_for_statistical_review"
            label = "样本达到统计展示门槛"
            warnings: list[str] = ["达到样本门槛不等于策略已稳定；仍需查看 Wilson 区间、Brier 分数、同行相对表现和滚动退化。"]
        elif sample_count:
            status = "sample_insufficient"
            label = f"样本不足 {sample_count} / {OBSERVATION_MIN_SAMPLES}"
            warnings = ["样本不足时模型信心不能写成统计胜率。"]
        else:
            status = "not_started"
            label = "尚无已到期且用户确认的样本"
            warnings = ["没有验证样本时只能形成待确认观察，不能宣称历史胜率。"]
        return {
            "applicable": True,
            "status": status,
            "sample_count": sample_count,
            "minimum_samples": OBSERVATION_MIN_SAMPLES,
            "statistical_claim_allowed": qualified,
            "scorecard_version": scorecard_version,
            "required_scorecard_version": OBSERVATION_SCORECARD_VERSION,
            "pending_user_confirmation_count": pending,
            "warnings": warnings,
            "label": label,
        }

    @staticmethod
    def _portfolio_gate(
        snapshot: dict[str, Any],
        is_storage: bool,
        user_decision_gate: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not is_storage:
            return {
                "applicable": False,
                "ready": True,
                "status": "not_applicable",
                "confirmed_count": 0,
                "draft_count": 0,
                "blockers": [],
                "warnings": [],
                "label": "当前房间不使用模拟组合风险门",
            }
        portfolios = [
            item
            for item in (snapshot.get("paper_portfolios") or [])
            if isinstance(item, dict)
        ]
        decision_gate = user_decision_gate or {}
        current_action = str(decision_gate.get("action") or "")
        current_decision_id = str(decision_gate.get("decision_id") or "")
        packages = [
            item
            for item in (snapshot.get("decision_packages") or [])
            if isinstance(item, dict)
        ]
        current_package = next(
            (
                item
                for item in packages
                if str(item.get("package_id") or "") == current_decision_id
            ),
            None,
        )
        linked_ids = {
            str(event.get("resource_id") or "")
            for package in packages
            for event in (package.get("lineage") or [])
            if isinstance(event, dict)
            and event.get("resource_type") == "simulation.paper_portfolio"
        }
        legacy_count = sum(
            1 for portfolio in portfolios
            if str(portfolio.get("id") or "") not in linked_ids
        )
        if current_action != "support" or not current_decision_id:
            warnings = []
            if legacy_count:
                warnings.append(
                    f"现有 {legacy_count} 个未关联旧模拟组合仅作历史记录，不会满足当前决定门。"
                )
            return {
                "applicable": False,
                "ready": True,
                "status": "awaiting_user_support",
                "confirmed_count": 0,
                "draft_count": sum(
                    1
                    for portfolio in portfolios
                    if str(portfolio.get("status") or "").upper() != "CONFIRMED"
                ),
                "linked_count": 0,
                "legacy_unlinked_count": legacy_count,
                "decision_id": current_decision_id,
                "blockers": [],
                "warnings": warnings,
                "label": "用户支持候选后再建立关联模拟组合",
            }

        package_integrity_ok = bool(
            current_package
            and current_package.get("state") == "active"
            and current_package.get("integrity_ok") is True
        )
        current_events = [
            event
            for event in ((current_package or {}).get("lineage") or [])
            if isinstance(event, dict)
            and event.get("resource_type") == "simulation.paper_portfolio"
        ]
        latest_event_by_portfolio: dict[str, dict[str, Any]] = {}
        for event in current_events:
            resource_id = str(event.get("resource_id") or "")
            if resource_id:
                latest_event_by_portfolio[resource_id] = event
        current_portfolios = {
            str(portfolio.get("id") or ""): portfolio
            for portfolio in portfolios
            if str(portfolio.get("id") or "") in latest_event_by_portfolio
        }

        confirmed_ready: list[dict[str, Any]] = []
        unsafe_count = 0
        candidate_mapping_invalid_count = 0
        draft_count = 0
        for portfolio in portfolios:
            evaluation = portfolio.get("evaluation") or {}
            safe = (
                evaluation.get("execution_capability") == "none"
                and evaluation.get("live_trading_allowed") is False
            )
            if not safe:
                unsafe_count += 1
            resource_id = str(portfolio.get("id") or "")
            latest_event = latest_event_by_portfolio.get(resource_id)
            if not latest_event:
                continue
            candidate_binding = (
                portfolio.get("candidate_simulation_binding")
                if isinstance(
                    portfolio.get("candidate_simulation_binding"), dict
                )
                else {}
            )
            candidate_mapping_required = bool(
                portfolio.get("candidate_simulation_contract")
            ) or candidate_binding.get("applicable") is True
            candidate_mapping_ready = bool(
                not candidate_mapping_required
                or candidate_binding.get("ready") is True
            )
            if candidate_mapping_required and not candidate_mapping_ready:
                candidate_mapping_invalid_count += 1
            exact_revision = str(portfolio.get("version") or "") == str(
                latest_event.get("resource_revision") or ""
            )
            current_resource_snapshot = {
                key: value
                for key, value in portfolio.items()
                if key not in {
                    "candidate_simulation_binding",
                    "candidate_simulation_contract_self_integrity_ok",
                    "candidate_simulation_status",
                }
            }
            exact_snapshot = latest_event.get(
                "resource_snapshot"
            ) == current_resource_snapshot
            event_confirmed = (
                latest_event.get("relation_type") == "confirms"
                and str(latest_event.get("resource_state") or "").upper() == "CONFIRMED"
                and latest_event.get("integrity_ok") is True
            )
            if str(portfolio.get("status") or "").upper() != "CONFIRMED":
                draft_count += 1
            elif (
                safe
                and candidate_mapping_ready
                and (evaluation.get("risk_gate") or {}).get("ready")
                and exact_revision
                and exact_snapshot
                and event_confirmed
            ):
                confirmed_ready.append(portfolio)
        blockers = []
        if not package_integrity_ok:
            blockers.append(ConvergenceService._issue(
                "DECISION_PACKAGE_INTEGRITY_FAILED",
                "当前决定包不存在、已失效或完整性校验失败",
                "刷新当前确认产物和用户决定；若哈希链损坏，保留审计记录并建立新的决定包。",
                "portfolio",
            ))
        if unsafe_count:
            blockers.append(ConvergenceService._issue(
                "PORTFOLIO_EXECUTION_BOUNDARY_BROKEN",
                "模拟组合越过只读边界",
                "停止收敛并恢复 execution_capability=none / live_trading_allowed=false。",
                "portfolio",
            ))
        if candidate_mapping_invalid_count:
            blockers.append(ConvergenceService._issue(
                "CANDIDATE_SIMULATION_BINDING_FAILED",
                "关联组合未通过候选语义合同校验",
                "重新打开当前决定包，核对候选标的、方向、期限、依据、失效条件和纸面权重后生成新版本。",
                "portfolio",
            ))
        if not current_events:
            blockers.append(ConvergenceService._issue(
                "DECISION_PACKAGE_PORTFOLIO_MISSING",
                "当前支持决定尚未关联模拟组合",
                "从当前决定包建立模拟组合，并记录它如何实现用户选择的候选方案。",
                "portfolio",
            ))
        elif not confirmed_ready:
            latest = next(iter(current_portfolios.values()), {})
            first_risk_blocker = (
                ((latest.get("evaluation") or {}).get("risk_gate") or {}).get("blockers") or [{}]
            )[0]
            blockers.append(ConvergenceService._issue(
                "DECISION_PACKAGE_PORTFOLIO_NOT_CONFIRMED",
                "决定包中尚无通过风险门的精确已确认模拟组合版本",
                str(first_risk_blocker.get("title") or "先复算风险、处理预算超限，再由用户确认当前关联版本。"),
                "portfolio",
            ))
        warnings = []
        if legacy_count:
            warnings.append(
                f"另有 {legacy_count} 个未关联旧模拟组合，仅保留历史，不计入当前决定包。"
            )
        ready = bool(confirmed_ready) and not blockers
        return {
            "applicable": True,
            "ready": ready,
            "status": "linked_confirmed" if ready else "review_required",
            "confirmed_count": len(confirmed_ready),
            "draft_count": draft_count,
            "linked_count": len(current_portfolios),
            "candidate_mapping_invalid_count": candidate_mapping_invalid_count,
            "legacy_unlinked_count": legacy_count,
            "decision_id": current_decision_id,
            "blockers": blockers,
            "warnings": warnings,
            "label": "决定包模拟组合已通过风险复核" if ready else "决定包模拟组合仍需关联或复核",
        }

    @staticmethod
    def _issue(code: str, title: str, detail: str, gate: str) -> dict[str, str]:
        return {"code": code, "title": title, "detail": detail, "gate": gate}
