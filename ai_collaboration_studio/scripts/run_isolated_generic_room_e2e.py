from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any


# Direct invocations default to the disposable/local environment.  A real
# run must explicitly set AI_STUDIO_SKIP_LOCAL_ENV=0 in addition to the
# existing paid-call acknowledgement.
os.environ.setdefault("AI_STUDIO_SKIP_LOCAL_ENV", "1")

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from scripts.run_isolated_12_role_e2e import (  # noqa: E402
    ArtifactGateFailed,
    BudgetedProvider,
    CallLedger,
    OpenAIForbidden,
    ProviderCallBudgetExceeded,
    ProviderGateFailed,
    RoundGateFailed,
    SourceDatabaseChanged,
    SourceRoomInvalid,
    _artifact_evidence,
    _emit_report,
    _readonly_sqlite_uri,
    _safe_error,
    _validated_report_file,
    build_dry_provider,
    build_registry,
    database_fingerprint,
    source_write_state_unchanged,
)


DEFAULT_SOURCE_DB = PROJECT_DIR / "runtime" / "collaboration_studio.sqlite3"
SOURCE_ROOM_ID = "room_plan"
EXPECTED_MEMBER_COUNT = 4
MAX_PROVIDER_CALLS = 16
MAX_WALL_SECONDS = 10 * 60
REAL_RUN_ACK = "MAX_16_PROVIDER_CALLS"
ROUND_OBJECTIVE = (
    "比较至少两个方向不同的方案，明确各自收益、风险、证据缺口和失效条件；"
    "由主持人动态选择下一位发言者，形成一个需用户核验的首选候选和选择理由。"
)


def read_source_room(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    if not path.is_file():
        raise SourceRoomInvalid("正式数据库不存在。")
    connection = sqlite3.connect(_readonly_sqlite_uri(path), uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        query_only = bool(connection.execute("PRAGMA query_only").fetchone()[0])
        changes_before = int(connection.total_changes)
        room_row = connection.execute(
            """SELECT
                   id,title,objective,domain,category,template_id,discussion_mode,
                   workflow_policy_json,capability_packs_json
               FROM rooms
              WHERE id=?""",
            (SOURCE_ROOM_ID,),
        ).fetchone()
        if not room_row:
            raise SourceRoomInvalid("正式数据库中不存在 room_plan。")
        member_rows = connection.execute(
            """SELECT
                   id,name,identity,instructions,responsibilities,boundaries,stance,
                   workflow_stage,capabilities_json,provider,model,enabled,position
               FROM members
              WHERE room_id=?
              ORDER BY position,id""",
            (SOURCE_ROOM_ID,),
        ).fetchall()
        changes_after = int(connection.total_changes)
    finally:
        connection.close()

    room = dict(room_row)
    try:
        workflow_policy = json.loads(str(room.pop("workflow_policy_json") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SourceRoomInvalid("通用房间工作流政策不是有效 JSON。") from exc
    room["workflow_policy"] = workflow_policy
    try:
        capability_pack_ids = json.loads(str(room.pop("capability_packs_json") or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SourceRoomInvalid("通用房间能力包配置不是有效 JSON。") from exc
    if not isinstance(capability_pack_ids, list):
        raise SourceRoomInvalid("通用房间能力包配置必须是数组。")
    room["capability_pack_ids"] = [
        str(item).strip() for item in capability_pack_ids if str(item).strip()
    ]
    members: list[dict[str, Any]] = []
    for row in member_rows:
        member = dict(row)
        try:
            capabilities = json.loads(str(member.pop("capabilities_json") or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            capabilities = []
        member["capabilities"] = capabilities if isinstance(capabilities, list) else []
        member["enabled"] = bool(member.get("enabled"))
        members.append(member)
    return {"room": room, "members": members}, {
        "query_only": query_only,
        "total_changes_before": changes_before,
        "total_changes_after": changes_after,
    }


def validate_source_room(source: dict[str, Any]) -> None:
    room = source.get("room") if isinstance(source.get("room"), dict) else {}
    members = source.get("members") if isinstance(source.get("members"), list) else []
    if room.get("id") != SOURCE_ROOM_ID or room.get("template_id") != "open_collaboration":
        raise SourceRoomInvalid("源房间必须是 room_plan 通用协作模板。")
    if room.get("discussion_mode") != "dynamic":
        raise SourceRoomInvalid("源房间没有启用动态主持。")
    if len(members) != EXPECTED_MEMBER_COUNT or any(not item.get("enabled") for item in members):
        raise SourceRoomInvalid("源房间必须恰好包含四位启用成员。")
    if len({str(item.get("id") or "") for item in members}) != EXPECTED_MEMBER_COUNT:
        raise SourceRoomInvalid("源房间成员 ID 不唯一。")
    if any(str(item.get("provider") or "").lower() == "openai" for item in members):
        raise OpenAIForbidden("源房间存在 OpenAI 路由。")
    for member in members:
        if not all(str(member.get(field) or "").strip() for field in (
            "name", "identity", "responsibilities", "boundaries", "stance", "workflow_stage",
        )):
            raise SourceRoomInvalid("源房间存在不完整的身份、职责或边界。")
    if not any(str(item.get("stance") or "") == "facilitator" for item in members):
        raise SourceRoomInvalid("源房间缺少主持人。")
    if not any(
        str(item.get("stance") or "") in {"challenger", "skeptic", "risk_reviewer"}
        or "critical_review" in (item.get("capabilities") or [])
        for item in members
    ):
        raise SourceRoomInvalid("源房间缺少反方或风险审查职责。")
    policy = room.get("workflow_policy") if isinstance(room.get("workflow_policy"), dict) else {}
    if (
        policy.get("execution_capability") != "none"
        or policy.get("live_trading_allowed") is not False
        or policy.get("user_confirmation_required") is not True
    ):
        raise SourceRoomInvalid("源房间用户确认与无执行边界不完整。")


def _clone_room(store: Any, source: dict[str, Any]) -> tuple[str, list[dict[str, Any]], dict[str, int]]:
    from backend.config import ARK_MODEL, DEEPSEEK_MODEL
    from backend.workflow_policy import policy_from_json, validate_workflow_policy

    policy = policy_from_json(source["room"].get("workflow_policy"), "open_collaboration")
    policy["minimum_successful_members"] = EXPECTED_MEMBER_COUNT
    policy["max_turns_per_member"] = 2
    policy["follow_up_budget"] = 1
    configured_stage_counts = Counter(
        str(item.get("workflow_stage") or "")
        for item in source["members"]
        if str(item.get("workflow_stage") or "") in set(policy.get("stage_order") or [])
    )
    policy["minimum_stage_coverage"] = {
        stage: int(configured_stage_counts.get(stage) or 0)
        for stage in policy.get("stage_order") or []
        if int(configured_stage_counts.get(stage) or 0) > 0
    }
    policy["execution_capability"] = "none"
    policy["live_trading_allowed"] = False
    policy["user_confirmation_required"] = True
    policy = validate_workflow_policy(policy)

    created = store.create_room(
        "通用多方案共创 · 隔离真实验收",
        ROUND_OBJECTIVE,
        domain="open_collaboration",
        category="通用共创",
        template_id="open_collaboration",
        workflow_policy=policy,
        # Keep the formal source room's exact optional-pack configuration. The
        # response graph is a new-round kernel protocol, not a test-only pack.
        capability_pack_ids=list(source["room"].get("capability_pack_ids") or []),
    )
    room_id = str((created.get("room") or {}).get("id") or "")
    if not room_id:
        raise SourceRoomInvalid("无法创建隔离通用房间。")
    for member in created.get("members") or []:
        store.delete_member(room_id, str(member.get("id") or ""))

    cloned: list[dict[str, Any]] = []
    for index, source_member in enumerate(source["members"]):
        provider = "doubao" if index == EXPECTED_MEMBER_COUNT - 1 else "deepseek"
        model = ARK_MODEL if provider == "doubao" else DEEPSEEK_MODEL
        member = store.add_member(room_id, {
            "name": source_member.get("name"),
            "identity": source_member.get("identity"),
            "instructions": source_member.get("instructions"),
            "responsibilities": source_member.get("responsibilities"),
            "boundaries": source_member.get("boundaries"),
            "stance": source_member.get("stance"),
            "workflow_stage": source_member.get("workflow_stage"),
            "capabilities": source_member.get("capabilities") or [],
            "provider": provider,
            "model": model,
            "enabled": True,
        })
        if not member:
            raise SourceRoomInvalid("无法复制通用房间成员。")
        cloned.append(member)
    cloned = store.reorder_members(room_id, [str(item["id"]) for item in cloned])
    provider_counts = Counter(str(item.get("provider") or "") for item in cloned)
    if len(cloned) != EXPECTED_MEMBER_COUNT or provider_counts != Counter({"deepseek": 3, "doubao": 1}):
        raise SourceRoomInvalid("隔离成员或双 Provider 路由不完整。")
    return room_id, cloned, dict(sorted(provider_counts.items()))


def _collect_round(orchestrator: Any, room_id: str, member_ids: list[str]) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    decisions: list[dict[str, str]] = []
    final: dict[str, Any] | None = None
    stream = orchestrator.run_round(room_id, ROUND_OBJECTIVE, member_ids)
    try:
        for event in stream:
            event_type = str(event.get("type") or "")
            if event_type == "message":
                message = event.get("message") or {}
                turn_contract = (
                    message.get("turn_contract")
                    if isinstance(message.get("turn_contract"), dict)
                    else {}
                )
                messages.append({
                    "id": str(message.get("id") or ""),
                    "sender_id": str(message.get("sender_id") or ""),
                    "provider": str(message.get("provider") or "").lower(),
                    "reply_to_message_id": str(message.get("reply_to_message_id") or ""),
                    "responds_to_ids": [
                        str(item.get("id") or "")
                        for item in turn_contract.get("responds_to") or []
                        if isinstance(item, dict) and str(item.get("id") or "")
                    ],
                    "turn_contract_version": message.get("turn_contract_version"),
                    "turn_contract_qualified": message.get("turn_contract_qualified") is True,
                    "hidden_block_leaked": (
                        "<turn_contract" in str(message.get("content") or "").lower()
                    ),
                })
            elif event_type == "director_decision":
                decisions.append({
                    "action": str(event.get("action") or ""),
                    "source": str(event.get("source") or ""),
                    "stage": str(event.get("stage") or ""),
                })
            elif event_type in {"error", "speaker_failed"}:
                code = str(event.get("code") or event.get("error_code") or "ROUND_EVENT_FAILED")
                raise RoundGateFailed(f"通用轮次事件失败：{code}。")
            elif event_type == "round_completed":
                final = {
                    "round_id": str(event.get("round_id") or ""),
                    "status": str(event.get("status") or ""),
                    "completed": int(event.get("completed") or 0),
                    "failures": int(event.get("failures") or 0),
                    "skipped": int(event.get("skipped") or 0),
                }
    finally:
        stream.close()
    if not final:
        raise RoundGateFailed("通用轮次没有产生完成事件。")
    return {"final": final, "messages": messages, "decisions": decisions}


def _run_isolated(source: dict[str, Any], mode: str, ledger: CallLedger) -> dict[str, Any]:
    temp_db_path: Path | None = None
    with tempfile.TemporaryDirectory(prefix="ai-studio-generic-e2e-") as temp_dir:
        temp_root = Path(temp_dir).resolve()
        temp_db_path = temp_root / "generic.sqlite3"
        if mode == "dry-run":
            os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"
        os.environ["AI_STUDIO_RUNTIME_DIR"] = str(temp_root)
        os.environ["AI_STUDIO_DATABASE_PATH"] = str(temp_db_path)

        from backend.artifact_service import ArtifactService
        from backend.convergence import ConvergenceService
        from backend.orchestrator import DiscussionOrchestrator
        from backend.provider_preflight import ProviderPreflightService
        from backend.providers.base import ProviderProbeResult, ProviderResponse
        from backend.providers.deepseek_provider import DeepSeekProvider
        from backend.providers.doubao_provider import DoubaoProvider
        from backend.providers.registry import ProviderRegistry
        from backend.store import StudioStore
        from backend.turn_contract import TURN_CONTRACT_VERSION
        from backend.turn_contract_artifact import project_turn_contract_artifact

        store = StudioStore(temp_db_path)
        room_id, members, route_counts = _clone_room(store, source)
        isolated_snapshot = store.room_snapshot(room_id) or {}
        isolated_room = (
            isolated_snapshot.get("room")
            if isinstance(isolated_snapshot.get("room"), dict)
            else {}
        )
        isolated_pack_ids = list(isolated_room.get("capability_pack_ids") or [])
        isolated_capabilities = list(isolated_room.get("capabilities") or [])
        source_pack_ids = list(source["room"].get("capability_pack_ids") or [])
        if isolated_pack_ids != source_pack_ids:
            raise SourceRoomInvalid("隔离通用房间没有保留正式源房间的能力包配置。")
        member_ids = [str(item["id"]) for item in members]
        if mode == "dry-run":
            raw_providers = {
                "deepseek": build_dry_provider(
                    "deepseek", "deepseek-v4-pro",
                    provider_response_class=ProviderResponse,
                    provider_probe_result_class=ProviderProbeResult,
                    fixture_profile="generic",
                ),
                "doubao": build_dry_provider(
                    "doubao", "doubao-seed-2-0-lite-260215",
                    provider_response_class=ProviderResponse,
                    provider_probe_result_class=ProviderProbeResult,
                    fixture_profile="generic",
                ),
            }
            external = False
        else:
            raw_providers = {"deepseek": DeepSeekProvider(), "doubao": DoubaoProvider()}
            external = True
        wrapped = {
            provider_id: BudgetedProvider(provider, ledger, external=external)
            for provider_id, provider in raw_providers.items()
        }
        registry = build_registry(
            provider_registry_class=ProviderRegistry,
            providers=wrapped,
            ledger=ledger,
        )
        preflight = ProviderPreflightService(store, registry).check_room(
            room_id,
            member_ids=member_ids,
            skip_provider_ids={"openai"},
        )
        if (
            not preflight.get("ready")
            or int(preflight.get("provider_check_count") or 0) != 2
            or preflight.get("unavailable_members")
        ):
            raise ProviderGateFailed("DeepSeek/豆包通用房间预检未全部通过。")

        orchestrator = DiscussionOrchestrator(store, registry, market_service=None)
        round_result = _collect_round(orchestrator, room_id, member_ids)
        final = round_result["final"]
        messages = round_result["messages"]
        decisions = round_result["decisions"]
        unique_members = {item["sender_id"] for item in messages if item["sender_id"]}
        first_turn_routes = Counter(item["provider"] for item in messages[:EXPECTED_MEMBER_COUNT])
        qualified_turn_contract_count = sum(
            1 for item in messages if item.get("turn_contract_qualified") is True
        )
        qualified_member_ids = {
            str(item.get("sender_id") or "")
            for item in messages
            if item.get("turn_contract_qualified") is True
            and str(item.get("sender_id") or "")
        }
        unqualified_turn_contract_count = sum(
            1 for item in messages
            if item.get("turn_contract_version") == TURN_CONTRACT_VERSION
            and item.get("turn_contract_qualified") is not True
        )
        hidden_block_leak_count = sum(
            1 for item in messages if item.get("hidden_block_leaked") is True
        )
        prior_ai_message_ids: set[str] = set()
        validated_response_edge_count = 0
        for item in messages:
            if prior_ai_message_ids:
                reply_target = str(item.get("reply_to_message_id") or "")
                contract_targets = set(item.get("responds_to_ids") or [])
                if reply_target in prior_ai_message_ids and reply_target in contract_targets:
                    validated_response_edge_count += 1
            message_id = str(item.get("id") or "")
            if message_id:
                prior_ai_message_ids.add(message_id)
        required_response_edge_count = max(0, len(messages) - 1)
        if not all((
            final["status"] == "COMPLETED",
            final["failures"] == 0,
            final["skipped"] == 0,
            final["completed"] in {4, 5},
            len(unique_members) == EXPECTED_MEMBER_COUNT,
            first_turn_routes == Counter(route_counts),
            qualified_turn_contract_count == final["completed"],
            len(qualified_member_ids) == EXPECTED_MEMBER_COUNT,
            unqualified_turn_contract_count == 0,
            hidden_block_leak_count == 0,
            validated_response_edge_count == required_response_edge_count,
            any(
                item["source"] in {"ai", "rules_first"}
                and item["action"] == "speak"
                for item in decisions
            ),
        )):
            raise RoundGateFailed("通用多 AI 轮次未满足覆盖、发言合同与动态主持门。")

        round_id = final["round_id"]
        round_record = store.get_round(room_id, round_id) or {}
        checkpoint = store.get_round_checkpoint(room_id, round_id)
        checkpoint_state = (checkpoint or {}).get("state") or {}
        if not all((
            checkpoint_state.get("turn_contract_version") == TURN_CONTRACT_VERSION,
            checkpoint_state.get("turn_contract_required") is True,
            checkpoint_state.get("capability_pack_ids") == source_pack_ids,
        )):
            raise RoundGateFailed("通用轮次没有独立于可选能力包冻结内核发言合同。")
        contract_bundle = store.round_turn_contract_bundle(room_id, round_id)
        if not all((
            contract_bundle.get("applicable") is True,
            contract_bundle.get("valid") is True,
            contract_bundle.get("turn_contract_version") == TURN_CONTRACT_VERSION,
            len(contract_bundle.get("messages") or []) == final["completed"],
            len(contract_bundle.get("successful_member_ids") or []) == EXPECTED_MEMBER_COUNT,
        )):
            issues = ",".join(str(item) for item in contract_bundle.get("issues") or [])
            raise RoundGateFailed("通用轮次持久化发言合同账本无效：" + (issues or "数量不匹配"))
        contract_projection = project_turn_contract_artifact(
            list(contract_bundle.get("messages") or []),
            member_resolver=lambda member_id, version: store.get_member_version(
                room_id, member_id, version
            ),
        )
        projected_decision = (
            contract_projection.get("decision")
            if isinstance(contract_projection.get("decision"), dict)
            else {}
        )
        projected_options = [
            item for item in projected_decision.get("options") or [] if isinstance(item, dict)
        ]
        projected_option_ids = [str(item.get("id") or "") for item in projected_options]
        projected_preferred = str(projected_decision.get("preferred_option_id") or "")
        if not all((
            contract_projection.get("qualified_message_count") == final["completed"],
            projected_decision.get("status") == "candidate",
            len(projected_options) >= 2,
            bool(projected_preferred),
            projected_preferred in projected_option_ids,
            bool(str(projected_decision.get("rationale") or "").strip()),
        )):
            raise ArtifactGateFailed("结构化发言合同没有确定性投影出完整的候选决策板。")
        before_artifact = ConvergenceService(store).evaluate(room_id, round_id=round_id)
        if not all((
            before_artifact.get("decision_status") == "DRAFT_REQUIRED",
            before_artifact.get("can_host_finish") is True,
            before_artifact.get("can_present_candidate_best") is False,
            (before_artifact.get("turn_contract_gate") or {}).get("ready") is True,
        )):
            raise RoundGateFailed("通用轮次的产物前收敛状态无效。")
        artifact = ArtifactService(store, registry).generate_minutes(room_id, round_id)
        content = artifact.get("content") if isinstance(artifact.get("content"), dict) else {}
        decision = content.get("decision") if isinstance(content.get("decision"), dict) else {}
        options = [item for item in decision.get("options") or [] if isinstance(item, dict)]
        option_ids = {str(item.get("id") or "") for item in options if str(item.get("id") or "")}
        artifact_option_ids = [str(item.get("id") or "") for item in options]
        generation_source = str(artifact.get("generation_source") or "")
        base_generation_source = generation_source.split("+", 1)[0]
        deterministic_decision_projection = all((
            artifact_option_ids == projected_option_ids,
            decision.get("status") == projected_decision.get("status"),
            decision.get("preferred_option_id") == projected_decision.get("preferred_option_id"),
            decision.get("rationale") == projected_decision.get("rationale"),
        ))
        evidence = _artifact_evidence(content)
        artifact_checks = {
            "draft": artifact.get("status") == "DRAFT",
            "round_bound": artifact.get("round_id") == round_id,
            "model_generated": base_generation_source != "template_fallback",
            "contract_projection_recorded": generation_source.endswith(
                f"+{TURN_CONTRACT_VERSION}"
            ),
            "deterministic_decision_projection": deterministic_decision_projection,
            "candidate_status": decision.get("status") == "candidate",
            "two_options": len(options) >= 2,
            "preferred_valid": str(decision.get("preferred_option_id") or "") in option_ids,
            "rationale_present": bool(str(decision.get("rationale") or "").strip()),
            "evidence_present": bool(evidence),
            "evidence_unreviewed": bool(evidence) and all(
                str(item.get("verification_status") or "") == "unreviewed" for item in evidence
            ),
        }
        failed_artifact_checks = [key for key, ready in artifact_checks.items() if not ready]
        if failed_artifact_checks:
            raise ArtifactGateFailed(
                "通用会议产物结构门失败：" + ",".join(failed_artifact_checks) +
                f"；option_count={len(options)}；evidence_count={len(evidence)}。"
            )
        after_artifact = ConvergenceService(store).evaluate(room_id, round_id=round_id)
        if not all((
            after_artifact.get("decision_status") == "EVIDENCE_REVIEW_REQUIRED",
            after_artifact.get("can_present_candidate_best") is False,
            after_artifact.get("can_autonomously_decide") is False,
            (after_artifact.get("evidence_gate") or {}).get("artifact_status") == "DRAFT",
        )):
            raise ArtifactGateFailed("通用多方案草稿没有保持用户核验门。")

        # Exercise the complete user-gated artifact lifecycle in isolation.  This
        # is a fixture-side simulation of an explicit human review, never an AI
        # self-confirmation and never a mutation of the source database.
        reviewed_content = json.loads(json.dumps(content, ensure_ascii=False))
        reviewed_evidence = _artifact_evidence(reviewed_content)
        for ref in reviewed_evidence:
            ref.pop("role", None)
            ref["evidence_role"] = "support"
            ref["verification_status"] = "source_checked"
            ref["review_note"] = "隔离验收：模拟用户逐条核验来源后确认。"
        counter_message_ids = []
        for contract_message in contract_bundle.get("messages") or []:
            member_snapshot = (
                contract_message.get("member_snapshot")
                if isinstance(contract_message.get("member_snapshot"), dict)
                else {}
            )
            stance = str(member_snapshot.get("stance") or "").strip().lower()
            stage = str(member_snapshot.get("workflow_stage") or "").strip().lower()
            capabilities = {
                str(item or "").strip().lower()
                for item in member_snapshot.get("capabilities") or []
                if str(item or "").strip()
            }
            if (
                stance in {"bear", "challenger", "risk"}
                or stage in {"debate", "risk"}
                or capabilities.intersection({"bear_case", "critical_review", "risk_review"})
            ):
                counter_message_ids.append(str(contract_message.get("id") or ""))
        summary_evidence = reviewed_content.setdefault("summary_evidence", [])
        summary_source_ids = {
            str(ref.get("id") or "")
            for ref in summary_evidence
            if isinstance(ref, dict)
        }
        counter_source_id = next((
            message_id
            for message_id in counter_message_ids
            if message_id and message_id not in summary_source_ids
        ), "")
        if not counter_source_id:
            raise ArtifactGateFailed("隔离轮次没有可追加到纪要的合格反证消息。")
        summary_evidence.append({
            "type": "message",
            "id": counter_source_id,
            "evidence_role": "counter",
            "verification_status": "source_checked",
            "review_note": "隔离验收：用户保留该反证及其失效条件。",
        })
        for risk in reviewed_content.get("risks") or []:
            if isinstance(risk, dict):
                risk["status"] = "accepted"
                risk["blocking"] = False
        for disagreement in reviewed_content.get("disagreements") or []:
            if isinstance(disagreement, dict):
                disagreement["status"] = "accepted_risk"
                disagreement["blocking"] = False
                disagreement["resolution"] = (
                    str(disagreement.get("resolution") or "")
                    or "隔离验收：用户确认保留分歧，不据此自动执行。"
                )
        reviewed_artifact = store.update_artifact(room_id, artifact["id"], {
            "expected_version": artifact["version"],
            "title": artifact.get("title") or "会议纪要草稿",
            "content": reviewed_content,
        })
        # Status, mitigation, ownership and resolution are part of the claim
        # that evidence was reviewed against.  The first save therefore resets
        # those affected relations.  Simulate the user's explicit second pass
        # over the exact saved revision before allowing confirmation.
        reviewed_content = json.loads(json.dumps(
            reviewed_artifact.get("content") or {},
            ensure_ascii=False,
        ))
        for ref in _artifact_evidence(reviewed_content):
            if str(ref.get("verification_status") or "") == "unreviewed":
                ref["verification_status"] = "source_checked"
                ref["review_note"] = (
                    "隔离验收：已按风险与分歧处理后的精确保存版本重新核验。"
                )
        reviewed_artifact = store.update_artifact(room_id, artifact["id"], {
            "expected_version": reviewed_artifact["version"],
            "title": reviewed_artifact.get("title") or "会议纪要草稿",
            "content": reviewed_content,
        })
        if not reviewed_artifact or not (
            reviewed_artifact.get("evidence_review") or {}
        ).get("confirmation_ready"):
            review_issues = (
                (reviewed_artifact or {}).get("evidence_review") or {}
            ).get("confirmation_issues") or []
            raise ArtifactGateFailed(
                "模拟用户复核后，会议产物仍未通过确认门："
                + "、".join(str(item) for item in review_issues[:6])
            )
        confirmed_artifact = store.confirm_artifact(
            room_id,
            artifact["id"],
            expected_version=int(reviewed_artifact["version"]),
            confirmed_by="isolated_fixture_user",
        )
        if not confirmed_artifact or confirmed_artifact.get("status") != "CONFIRMED":
            raise ArtifactGateFailed("模拟用户确认没有形成绑定版本的 CONFIRMED 产物。")
        after_confirmation = ConvergenceService(store).evaluate(
            room_id,
            round_id=round_id,
        )
        if not all((
            after_confirmation.get("decision_status") == "READY_FOR_USER_DECISION",
            after_confirmation.get("can_present_candidate_best") is True,
            after_confirmation.get("can_autonomously_decide") is False,
            after_confirmation.get("user_confirmation_required") is True,
        )):
            raise ArtifactGateFailed("用户确认产物后没有进入保留最终决定权的候选展示态。")

        result = {
            "isolation": {
                "temporary_database": True,
                "temporary_database_removed": False,
            },
            "routing": {
                "source_preserved": True,
                "source_capability_pack_ids": list(
                    (source.get("room") or {}).get("capability_pack_ids") or []
                ),
                "isolated_capability_pack_ids": isolated_pack_ids,
                "isolated_turn_contract_capability": (
                    "discussion.turn_contract_v1" in isolated_capabilities
                ),
                "core_turn_contract_protocol": bool(
                    round_record.get("turn_contract_version") == TURN_CONTRACT_VERSION
                    and checkpoint_state.get("turn_contract_required") is True
                ),
                "isolated_acceptance_provider_counts": route_counts,
                "openai_assignments": 0,
            },
            "provider_preflight": {
                "ready": bool(preflight.get("ready")),
                "provider_check_count": int(preflight.get("provider_check_count") or 0),
                "member_count": int(preflight.get("member_count") or 0),
            },
            "round": {
                "status": final["status"],
                "completed_turns": final["completed"],
                "unique_successful_members": len(unique_members),
                "first_turn_provider_counts": dict(sorted(first_turn_routes.items())),
                "turn_contract_version": checkpoint_state.get("turn_contract_version"),
                "qualified_turn_contract_count": qualified_turn_contract_count,
                "qualified_unique_member_count": len(qualified_member_ids),
                "unqualified_turn_contract_count": unqualified_turn_contract_count,
                "hidden_block_leak_count": hidden_block_leak_count,
                "required_response_edge_count": required_response_edge_count,
                "validated_response_edge_count": validated_response_edge_count,
                "ai_speak_decisions": sum(
                    1 for item in decisions if item["source"] == "ai" and item["action"] == "speak"
                ),
                "rules_first_speak_decisions": sum(
                    1
                    for item in decisions
                    if item["source"] == "rules_first" and item["action"] == "speak"
                ),
                "ai_finish_decisions": sum(
                    1 for item in decisions if item["source"] == "ai" and item["action"] == "finish"
                ),
                "failures": final["failures"],
                "skipped": final["skipped"],
            },
            "artifact": {
                "status": confirmed_artifact.get("status"),
                "draft_status": artifact.get("status"),
                "reviewed_status": reviewed_artifact.get("status"),
                "round_bound": artifact.get("round_id") == round_id,
                "generation_provider": base_generation_source.split(":", 1)[0],
                "generation_mode": "external_provider" if external else "fixture_provider",
                "external_model_generated": bool(
                    external
                    and base_generation_source != "template_fallback"
                ),
                "turn_contract_projection_recorded": generation_source.endswith(
                    f"+{TURN_CONTRACT_VERSION}"
                ),
                "deterministic_decision_projection": deterministic_decision_projection,
                "projected_qualified_message_count": int(
                    contract_projection.get("qualified_message_count") or 0
                ),
                "projected_decision_option_count": len(projected_options),
                "decision_option_count": len(options),
                "preferred_option_recorded": str(decision.get("preferred_option_id") or "") in option_ids,
                "decision_rationale_recorded": bool(str(decision.get("rationale") or "").strip()),
                "evidence_count": len(evidence),
                "unreviewed_evidence_count": sum(
                    1 for item in evidence if str(item.get("verification_status") or "") == "unreviewed"
                ),
                "reviewed_evidence_count": len(_artifact_evidence(reviewed_content)),
                "evidence_review_ready": bool(
                    (reviewed_artifact.get("evidence_review") or {}).get("confirmation_ready")
                ),
                "confirmed": confirmed_artifact.get("status") == "CONFIRMED",
                "confirmed_by_fixture_user": (
                    confirmed_artifact.get("confirmed_by") == "isolated_fixture_user"
                ),
            },
            "convergence": {
                "before_artifact": before_artifact.get("decision_status"),
                "after_artifact": after_artifact.get("decision_status"),
                "after_confirmation": after_confirmation.get("decision_status"),
                "after_confirmation_can_present_candidate_best": bool(
                    after_confirmation.get("can_present_candidate_best")
                ),
                "discussion_ready": bool((after_artifact.get("discussion_gate") or {}).get("ready")),
                "turn_contract_ready": bool(
                    (after_artifact.get("turn_contract_gate") or {}).get("ready")
                ),
                "can_present_candidate_best": bool(after_artifact.get("can_present_candidate_best")),
                "can_autonomously_decide": bool(after_artifact.get("can_autonomously_decide")),
                "user_confirmation_required": bool(after_artifact.get("user_confirmation_required")),
            },
            "safety": {
                "execution_capability": after_artifact.get("execution_capability"),
                "live_trading_allowed": after_artifact.get("live_trading_allowed"),
                "openai_hard_forbidden": True,
                "provider_retries": False,
                "cross_provider_fallback": False,
            },
        }
    result["isolation"]["temporary_database_removed"] = bool(
        temp_db_path is not None and not temp_db_path.exists()
    )
    return result


def run_acceptance(source_db: Path, mode: str) -> dict[str, Any]:
    source_path = source_db.resolve()
    before = database_fingerprint(source_path)
    read_audit: dict[str, Any] = {"query_only": False, "total_changes_before": -1, "total_changes_after": -1}
    ledger = CallLedger(mode=mode, max_calls=MAX_PROVIDER_CALLS, wall_seconds=MAX_WALL_SECONDS)
    result: dict[str, Any] = {}
    error: dict[str, str] | None = None
    source_provider_counts: dict[str, int] = {}
    try:
        if int((((before.get("files") or {}).get("wal") or {}).get("size") or 0)) > 0:
            raise SourceDatabaseChanged("正式数据库存在未归并 WAL，无法建立不可变身份快照。")
        source, read_audit = read_source_room(source_path)
        after_read = database_fingerprint(source_path)
        if not source_write_state_unchanged(before, after_read):
            raise SourceDatabaseChanged("正式数据库在只读身份快照期间发生变化。")
        validate_source_room(source)
        source_provider_counts = dict(sorted(Counter(
            str(item.get("provider") or "").lower() for item in source["members"]
        ).items()))
        result = _run_isolated(source, mode, ledger)
    except Exception as exc:
        error = _safe_error(exc)

    after = database_fingerprint(source_path)
    unchanged = source_write_state_unchanged(before, after)
    read_unchanged = (
        int(read_audit.get("total_changes_before") or 0)
        == int(read_audit.get("total_changes_after") or 0)
        == 0
    )
    if not unchanged or not read_unchanged:
        unchanged = False
        error = _safe_error(SourceDatabaseChanged("正式数据库前后指纹不一致，验收结果作废。"))
    report = {
        "schema_version": 1,
        "scenario": "generic_dynamic_decision_slate",
        "mode": mode,
        "ok": error is None,
        "source_database": {
            "query_only_asserted": bool(read_audit.get("query_only")),
            "read_connection_total_changes": int(read_audit.get("total_changes_after") or 0),
            "unchanged": unchanged,
            "main_sha256_before": ((before.get("files") or {}).get("main") or {}).get("sha256", ""),
            "main_sha256_after": ((after.get("files") or {}).get("main") or {}).get("sha256", ""),
        },
        "source_room": {
            "id": SOURCE_ROOM_ID,
            "expected_member_count": EXPECTED_MEMBER_COUNT,
            "provider_counts": source_provider_counts,
            "openai_assignments": int(source_provider_counts.get("openai", 0)),
        },
        "providers": ledger.summary(),
        **result,
    }
    if error is not None:
        report["error"] = error
    if report["providers"]["total_calls"] > MAX_PROVIDER_CALLS:
        report["ok"] = False
        report["error"] = _safe_error(ProviderCallBudgetExceeded("Provider 调用超过 16 次硬上限。"))
    if report["providers"]["openai_network_calls"] != 0:
        report["ok"] = False
        report["error"] = _safe_error(OpenAIForbidden("检测到 OpenAI 网络调用。"))
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="隔离的通用多 AI 动态决策板验收器。")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="使用本地假 Provider 验证完整流程。")
    mode.add_argument(
        "--execute-real",
        action="store_true",
        help=(
            "真实调用 DeepSeek 与豆包；必须同时设置 "
            "AI_STUDIO_SKIP_LOCAL_ENV=0 并提供付费调用确认短语。"
        ),
    )
    parser.add_argument("--acknowledge-paid-calls", default="")
    parser.add_argument("--source-db", type=Path, default=DEFAULT_SOURCE_DB)
    parser.add_argument("--report-file", type=Path, default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report_file = _validated_report_file(args.report_file)
    except ValueError as exc:
        _emit_report({
            "schema_version": 1,
            "scenario": "generic_dynamic_decision_slate",
            "mode": "real" if args.execute_real else "dry-run" if args.dry_run else "none",
            "ok": False,
            "error": {"code": "REPORT_FILE_INVALID", "message": str(exc)},
        }, None)
        return 2
    if not args.dry_run and not args.execute_real:
        _emit_report({
            "schema_version": 1,
            "scenario": "generic_dynamic_decision_slate",
            "mode": "none",
            "ok": False,
            "error": {"code": "MODE_REQUIRED", "message": "请显式选择 --dry-run 或 --execute-real。"},
        }, report_file)
        return 2
    if args.execute_real and args.acknowledge_paid_calls != REAL_RUN_ACK:
        _emit_report({
            "schema_version": 1,
            "scenario": "generic_dynamic_decision_slate",
            "mode": "real",
            "ok": False,
            "error": {
                "code": "PAID_CALL_ACK_REQUIRED",
                "message": f"真实运行必须精确填写 {REAL_RUN_ACK}。",
            },
        }, report_file)
        return 2
    if args.execute_real and os.environ.get(
        "AI_STUDIO_SKIP_LOCAL_ENV", ""
    ).strip().lower() not in {"0", "false", "no"}:
        _emit_report({
            "schema_version": 1,
            "scenario": "generic_dynamic_decision_slate",
            "mode": "real",
            "ok": False,
            "error": {
                "code": "REAL_ENV_OPT_IN_REQUIRED",
                "message": (
                    "真实运行还必须显式设置 AI_STUDIO_SKIP_LOCAL_ENV=0；"
                    "没有发起外部调用。"
                ),
            },
        }, report_file)
        return 2
    report = run_acceptance(args.source_db, "real" if args.execute_real else "dry-run")
    _emit_report(report, report_file)
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
