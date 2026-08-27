from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from contextlib import closing
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .decision_lineage import canonical_sha256
from .path_identity import first_reparse_component
from .readonly_mcp_gateway import (
    ReadOnlyManualChatGPTDataSource,
    ReadonlyMCPError,
    sanitize_gateway_value,
)


OPERATIONS_SUMMARY_VERSION = "manual_chatgpt_operations_summary_v1"
AB_REPLAY_DATASET_VERSION = "manual_chatgpt_ab_replay_dataset_v1"
AB_REPLAY_REPORT_VERSION = "manual_chatgpt_ab_replay_report_v1"
AB_SOURCE_SNAPSHOT_VERSION_V1 = "manual_chatgpt_ab_source_snapshot_v1"
AB_SOURCE_SNAPSHOT_VERSION_V2 = "manual_chatgpt_ab_source_snapshot_v2"
AB_SOURCE_SNAPSHOT_VERSION_V3 = "manual_chatgpt_ab_source_snapshot_v3"
AB_SOURCE_SNAPSHOT_VERSION = AB_SOURCE_SNAPSHOT_VERSION_V3
AB_COLLECTION_STATUS_VERSION = "manual_chatgpt_ab_collection_status_v1"
AB_ARM_EXPORT_VERSION = "manual_chatgpt_ab_arm_export_v1"
AB_SOURCE_REVIEW_ACKNOWLEDGEMENT = "I_REVIEWED_BOTH_AB_ARMS"
SCHEDULED_TASK_CONTRACT_VERSION = "manual_chatgpt_scheduled_task_contract_v1"
SCHEDULED_OPERATIONS_DATABASE_ENV = "AI_STUDIO_OPERATIONS_DATABASE"

MIN_AB_CASES = 20
MAX_AB_CASES = 30
MAX_SUMMARY_ITEMS = 100
MAX_SCANNED_SESSIONS = 10_000
MAX_SCANNED_PROVIDER_CALLS = 50_000
MAX_DATASET_BYTES = 2 * 1024 * 1024
MAX_AB_SOURCE_FILE_BYTES = 128 * 1024
MAX_AB_COLLECTION_SCAN_ENTRIES = MAX_AB_CASES + 1
INCOMPLETE_STATES = frozenset({
    "DRAFT",
    "BUNDLE_READY",
    "WAITING_FOR_CHATGPT",
    "RESULT_IMPORTED",
    "VALIDATING",
    "API_REVIEW",
    "READY_FOR_DECISION",
    "CONTEXT_STALE",
    "IMPORT_REJECTED",
    "BUDGET_BLOCKED",
    "NEEDS_USER_ACTION",
})
_AB_BASIS_VALUES = frozenset({
    "measured",
    "recorded",
    "estimated",
    "projected",
    "unavailable",
})
_AB_NUMERIC_METRICS = (
    "model_calls",
    "input_characters",
    "estimated_tokens",
    "api_cost_usd",
    "wait_ms",
    "human_operation_minutes",
)


class ManualChatGPTOperationsError(ValueError):
    def __init__(self, message: str, *, code: str = "OPERATIONS_INVALID") -> None:
        super().__init__(message)
        self.code = code


def build_scheduled_task_contract(
    *,
    timezone_name: str = "Asia/Shanghai",
    local_time: str = "09:00",
    waiting_expiry_hours: int = 24,
    max_items: int = 50,
) -> dict[str, Any]:
    """Build a path-free, non-installing contract for a desktop scheduled task."""

    clean_timezone = str(timezone_name or "").strip()
    try:
        ZoneInfo(clean_timezone)
    except ZoneInfoNotFoundError as exc:
        raise ManualChatGPTOperationsError(
            "The requested timezone is unavailable.",
            code="OPERATIONS_TIMEZONE_INVALID",
        ) from exc
    clean_local_time = str(local_time or "").strip()
    try:
        parsed_local_time = datetime.strptime(clean_local_time, "%H:%M")
    except ValueError as exc:
        raise ManualChatGPTOperationsError(
            "--local-time must use 24-hour HH:MM format.",
            code="SCHEDULED_LOCAL_TIME_INVALID",
        ) from exc
    if parsed_local_time.strftime("%H:%M") != clean_local_time:
        raise ManualChatGPTOperationsError(
            "--local-time must use zero-padded 24-hour HH:MM format.",
            code="SCHEDULED_LOCAL_TIME_INVALID",
        )
    if (
        isinstance(waiting_expiry_hours, bool)
        or not isinstance(waiting_expiry_hours, int)
        or not 1 <= waiting_expiry_hours <= 24 * 30
    ):
        raise ManualChatGPTOperationsError(
            "waiting_expiry_hours must be between 1 and 720.",
            code="OPERATIONS_EXPIRY_INVALID",
        )
    if (
        isinstance(max_items, bool)
        or not isinstance(max_items, int)
        or not 1 <= max_items <= MAX_SUMMARY_ITEMS
    ):
        raise ManualChatGPTOperationsError(
            f"max_items must be between 1 and {MAX_SUMMARY_ITEMS}.",
            code="OPERATIONS_LIMIT_INVALID",
        )

    command_argv = [
        "python",
        "-m",
        "backend.manual_chatgpt_operations",
        "scheduled-daily-summary",
        "--timezone",
        clean_timezone,
        "--waiting-expiry-hours",
        str(waiting_expiry_hours),
        "--max-items",
        str(max_items),
        "--format",
        "markdown",
    ]
    task_prompt = "\n".join([
        "在已明确授权的 AI 共创室本地项目中生成只读运营日报。",
        "只运行 contract.command_argv 指定的命令；不要替换为其他脚本或服务。",
        f"运行环境必须由操作员预先设置 AI_STUDIO_SKIP_LOCAL_ENV=1 和 {SCHEDULED_OPERATIONS_DATABASE_ENV}。",
        "如果任一环境条件、项目目录、数据库只读访问或 schema 不满足，只报告失败代码并停止。",
        "不得导入 ChatGPT 结果，不得调用 Provider、Futu/OpenD、市场接口或 MCP，不得执行 migration，不得修改文件或数据库。",
        "把 stdout 中的 Markdown 原样展示给用户；不要把缺失费用改写为零，也不要自动处理任何待办。",
        "这是运营提醒，不是模型席位、投资建议、授权状态或外部权限证明。",
    ])
    contract: dict[str, Any] = {
        "version": SCHEDULED_TASK_CONTRACT_VERSION,
        "title": "AI 共创室早间只读运营摘要",
        "schedule_suggestion": {
            "cadence": "daily",
            "local_time": clean_local_time,
            "timezone": clean_timezone,
            "operator_confirmation_required": True,
        },
        "execution_surface": {
            "recommended": "chatgpt_desktop_scheduled_task",
            "selected_project_mode": "local_project",
            "local_project_supported": True,
            "isolated_worktree_requested": False,
            "isolated_worktree_requires_git_repository": True,
            "computer_and_app_must_remain_running": True,
            "web_task_can_directly_access_local_directory": False,
        },
        "command_argv": command_argv,
        "task_prompt": task_prompt,
        "required_environment": {
            "AI_STUDIO_SKIP_LOCAL_ENV": {
                "required_value": "1",
                "purpose": "禁止自动加载本地 Provider 或市场配置。",
            },
            SCHEDULED_OPERATIONS_DATABASE_ENV: {
                "required": True,
                "value_included_in_contract": False,
                "purpose": "由操作员单独绑定已批准的现有 SQLite；契约不携带路径。",
            },
        },
        "automation_boundary": {
            "report_only": True,
            "database_connection_mode": "ro_query_only",
            "database_write_capability": False,
            "provider_calls_allowed": False,
            "market_calls_allowed": False,
            "mcp_calls_allowed": False,
            "result_imports_allowed": False,
            "formal_migration_allowed": False,
            "automatic_follow_up_actions_allowed": False,
        },
        "product_assumptions": {
            "account_task_limit_assumed": False,
            "model_availability_assumed": False,
        },
        "external_state": {
            "external_task_created": False,
            "workspace_scheduled_tasks_enabled_verified": False,
            "local_project_access_verified": False,
            "python_runtime_verified": False,
            "database_access_verified": False,
            "first_run_reviewed": False,
        },
        "official_documentation": "https://learn.chatgpt.com/docs/automations",
    }
    contract["contract_sha256"] = canonical_sha256(contract)
    return contract


def _scheduled_database_from_environment() -> Path:
    if os.environ.get("AI_STUDIO_SKIP_LOCAL_ENV") != "1":
        raise ManualChatGPTOperationsError(
            "Scheduled operations require AI_STUDIO_SKIP_LOCAL_ENV=1.",
            code="SCHEDULED_ENV_ISOLATION_REQUIRED",
        )
    raw_path = os.environ.get(SCHEDULED_OPERATIONS_DATABASE_ENV, "").strip()
    if not raw_path or "\x00" in raw_path:
        raise ManualChatGPTOperationsError(
            f"Scheduled operations require {SCHEDULED_OPERATIONS_DATABASE_ENV}.",
            code="SCHEDULED_DATABASE_REQUIRED",
        )
    return Path(raw_path)


def _ab_export_identifier(value: Any, label: str) -> str:
    clean = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", clean):
        raise ManualChatGPTOperationsError(
            f"{label} must be an ASCII identifier.",
            code="AB_ARM_EXPORT_SCOPE_INVALID",
        )
    return clean


def _citation_reference_count(value: Any) -> int:
    count = 0
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, Mapping):
            for key, item in current.items():
                if key == "evidence_refs" and isinstance(item, list):
                    count += sum(1 for reference in item if str(reference or "").strip())
                else:
                    stack.append(item)
        elif isinstance(current, list):
            stack.extend(current)
    return count


def build_manual_chatgpt_ab_arm_export(
    database_path: str | Path,
    *,
    room_id: Any,
    round_id: Any,
) -> dict[str, Any]:
    """Export one integrity-verified frozen Manual ChatGPT session as an A/B B arm."""

    clean_room_id = _ab_export_identifier(room_id, "room_id")
    clean_round_id = _ab_export_identifier(round_id, "round_id")
    data_source = ReadOnlyManualChatGPTDataSource(database_path)
    try:
        with closing(data_source._connect()) as connection:
            connection.execute("BEGIN")
            session_row = connection.execute(
                """SELECT * FROM manual_chatgpt_sessions
                     WHERE room_id=? AND round_id=?""",
                (clean_room_id, clean_round_id),
            ).fetchone()
            if not session_row:
                raise ManualChatGPTOperationsError(
                    "The requested Manual ChatGPT round was not found.",
                    code="AB_ARM_EXPORT_NOT_FOUND",
                )
            session_data = dict(session_row)
            session_id = str(session_data.get("id") or "")
            event_rows = [
                dict(row)
                for row in connection.execute(
                    """SELECT * FROM manual_chatgpt_events
                         WHERE session_id=? AND room_id=? ORDER BY sequence_no""",
                    (session_id, clean_room_id),
                ).fetchall()
            ]
            review_context = data_source._read_review_context(
                connection,
                session_id=session_id,
                room_id=clean_room_id,
            )
            projection = data_source._verified_projection(
                session_data,
                event_rows,
                review_context=review_context,
            )
    except sqlite3.Error as exc:
        raise ManualChatGPTOperationsError(
            "The read-only A/B arm source is unavailable.",
            code="AB_ARM_EXPORT_DATABASE_READ_FAILED",
        ) from exc

    if projection.get("state") != "FROZEN":
        raise ManualChatGPTOperationsError(
            "Only a fully frozen Manual ChatGPT session can be exported.",
            code="AB_ARM_EXPORT_NOT_FROZEN",
        )
    bundle = projection.get("bundle") if isinstance(projection.get("bundle"), Mapping) else {}
    budget = bundle.get("budget") if isinstance(bundle.get("budget"), Mapping) else {}
    planning = bundle.get("planning") if isinstance(bundle.get("planning"), Mapping) else {}
    context_size = (
        planning.get("context_size")
        if isinstance(planning.get("context_size"), Mapping)
        else {}
    )
    workload = (
        planning.get("workload")
        if isinstance(planning.get("workload"), Mapping)
        else {}
    )
    panel_calls = int(budget.get("chatgpt_panel_calls") or 0)
    completed_api_reviews = int(
        (projection.get("api_review") or {}).get("completed") or 0
    )
    if panel_calls not in {1, 2, 3} or completed_api_reviews not in {2, 3, 4}:
        raise ManualChatGPTOperationsError(
            "The frozen session has an invalid model-call budget.",
            code="AB_ARM_EXPORT_BUDGET_INVALID",
        )
    context_characters = int(context_size.get("characters") or 0)
    context_tokens = int(context_size.get("estimated_tokens") or 0)
    estimated_review_tokens = int(
        workload.get("estimated_api_review_input_tokens") or 0
    )
    if context_characters <= 0 or context_tokens <= 0 or estimated_review_tokens <= 0:
        raise ManualChatGPTOperationsError(
            "The frozen session lacks a complete deterministic input estimate.",
            code="AB_ARM_EXPORT_PLANNING_INVALID",
        )

    attempts = [
        dict(item)
        for item in review_context.get("attempts", [])
        if isinstance(item, Mapping)
    ]
    recorded_cost = Decimal(0)
    recorded_cost_calls = 0
    usage_integrity_failures = 0
    provider_elapsed_ms = 0
    for attempt in attempts:
        provider_elapsed_ms += max(0, int(attempt.get("elapsed_ms") or 0))
        usage = _json_object(attempt.get("usage_json"))
        if canonical_sha256(usage) != str(attempt.get("usage_sha256") or ""):
            usage_integrity_failures += 1
            continue
        cost = _finite_decimal(usage.get("cost_usd"))
        if cost is not None:
            recorded_cost += cost
            recorded_cost_calls += 1

    api_cost_usd: float | None = None
    api_cost_basis = "unavailable"
    api_cost_source = "unavailable"
    if attempts and recorded_cost_calls == len(attempts) and not usage_integrity_failures:
        api_cost_usd = float(_decimal_output(recorded_cost))
        api_cost_basis = "recorded"
        api_cost_source = "provider_call_attempt_usage_cost_usd"
    else:
        estimate = (
            planning.get("estimated_api_cost")
            if isinstance(planning.get("estimated_api_cost"), Mapping)
            else {}
        )
        estimated_amount = _finite_decimal(estimate.get("amount_usd"))
        if estimate.get("status") == "estimated" and estimated_amount is not None:
            api_cost_usd = float(_decimal_output(estimated_amount))
            api_cost_basis = "estimated"
            api_cost_source = "frozen_review_rate_card_estimate"

    waiting_at = next(
        (
            int(event.get("created_at") or 0)
            for event in projection.get("events", [])
            if event.get("to_state") == "WAITING_FOR_CHATGPT"
        ),
        0,
    )
    imported_at = next(
        (
            int(event.get("created_at") or 0)
            for event in projection.get("events", [])
            if event.get("to_state") == "RESULT_IMPORTED"
        ),
        0,
    )
    wait_ms = (
        imported_at - waiting_at
        if waiting_at > 0 and imported_at >= waiting_at
        else None
    )
    wait_basis = "measured" if wait_ms is not None else "unavailable"
    result = projection.get("result") if isinstance(projection.get("result"), Mapping) else {}
    citation_count = _citation_reference_count(result)
    decision = (
        dict(review_context.get("decision") or {})
        if isinstance(review_context.get("decision"), Mapping)
        else {}
    )
    selected_option_id = str(decision.get("selected_option_id") or "").strip()
    if not selected_option_id:
        raise ManualChatGPTOperationsError(
            "The frozen session lacks a selected final conclusion.",
            code="AB_ARM_EXPORT_DECISION_INVALID",
        )

    arm = {
        "model_calls": panel_calls + completed_api_reviews,
        "input_characters": context_characters * (
            panel_calls + completed_api_reviews
        ),
        "estimated_tokens": context_tokens * panel_calls + estimated_review_tokens,
        "api_cost_usd": api_cost_usd,
        "wait_ms": wait_ms,
        "human_operation_minutes": None,
        "citation_refs_total": citation_count,
        "citation_refs_passed": citation_count,
        "final_conclusion_id": selected_option_id,
        "basis": {
            "model_calls": "projected",
            "input_characters": "projected",
            "estimated_tokens": "estimated",
            "api_cost_usd": api_cost_basis,
            "wait_ms": wait_basis,
            "human_operation_minutes": "unavailable",
            "citations": "measured",
            "final_conclusion": "measured",
        },
    }
    export: dict[str, Any] = {
        "version": AB_ARM_EXPORT_VERSION,
        "room_id": clean_room_id,
        "round_id": clean_round_id,
        "session_id": str(projection.get("session_id") or ""),
        "source": {
            "state": "FROZEN",
            "mode": str(projection.get("mode") or ""),
            "bundle_sha256": str(projection.get("bundle_sha256") or ""),
            "result_sha256": str(projection.get("result_sha256") or ""),
            "event_head_sha256": str(session_data.get("event_head_sha256") or ""),
            "integrity_verified": True,
            "declared_historical_source_truth_verified": False,
        },
        "arm": arm,
        "metric_provenance": {
            "model_calls": {
                "chatgpt_panel_calls": panel_calls,
                "chatgpt_panel_calls_are_user_protocol_declarations": True,
                "completed_api_review_calls": completed_api_reviews,
                "api_review_calls_integrity_verified": True,
            },
            "input_characters": {
                "definition": "frozen_context_characters_times_declared_and_verified_calls",
                "context_characters": context_characters,
            },
            "estimated_tokens": {
                "chatgpt_context_tokens_per_panel": context_tokens,
                "estimated_api_review_input_tokens": estimated_review_tokens,
                "tokenizer_exact": False,
            },
            "api_cost_usd": {
                "source": api_cost_source,
                "provider_attempts": len(attempts),
                "attempts_with_recorded_cost_usd": recorded_cost_calls,
                "usage_integrity_failures": usage_integrity_failures,
                "manual_chatgpt_subscription_excluded": True,
            },
            "wait_ms": {
                "definition": "dispatch_to_import_event_chain_wall_time",
                "dispatch_to_import_ms": (
                    imported_at - waiting_at
                    if waiting_at > 0 and imported_at >= waiting_at
                    else None
                ),
                "provider_elapsed_ms_observed_not_included": provider_elapsed_ms,
                "provider_elapsed_ms_is_not_event_chain_sealed": True,
            },
            "human_operation_minutes": {
                "reason_unavailable": "active_human_time_is_not_measured_by_session_events",
                "must_not_be_inferred_from_wall_clock": True,
            },
            "citations": {
                "definition": "validated_evidence_ref_occurrences_in_imported_result",
                "zero_denominator_is_unavailable_in_replay": citation_count == 0,
            },
            "final_conclusion": {
                "definition": "user_confirmed_selected_option_id",
            },
        },
        "verification_boundary": {
            "database_connection_mode": "ro_query_only",
            "database_write_capability": False,
            "provider_calls_performed": 0,
            "market_calls_performed": 0,
            "result_imports_performed": 0,
            "formal_migration_performed": False,
            "ready_to_pair_with_reviewed_baseline_arm": True,
            "complete_ab_case": False,
        },
    }
    export["export_sha256"] = canonical_sha256(export)
    return export


def _json_object(raw: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(raw or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _strict_json_loads(raw: bytes) -> Any:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def reject_constant(_value: str) -> None:
        raise ValueError("non-finite JSON number")

    return json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=reject_duplicates,
        parse_constant=reject_constant,
    )


def _finite_decimal(value: Any, *, maximum: Decimal = Decimal("1000000000000000")) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        numeric = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not numeric.is_finite() or numeric < 0 or numeric > maximum:
        return None
    return numeric


def _decimal_output(value: Decimal, places: str = "0.000001") -> str:
    rounded = value.quantize(Decimal(places), rounding=ROUND_HALF_UP)
    return format(rounded, "f")


def _bounded_text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _summary_item(session: Mapping[str, Any], room_title: str, as_of_ms: int) -> dict[str, Any]:
    updated_at = int(session.get("updated_at") or 0)
    return {
        "room_id": str(session.get("room_id") or ""),
        "round_id": str(session.get("round_id") or ""),
        "session_id": str(session.get("session_id") or ""),
        "room_title": _bounded_text(sanitize_gateway_value(room_title), 160),
        "mode": str(session.get("mode") or ""),
        "state": str(session.get("state") or ""),
        "updated_at": updated_at,
        "age_minutes": max(0, (as_of_ms - updated_at) // 60_000),
    }


class DailyOperationsSummary:
    """Build a read-only local operating report without invoking any provider."""

    def __init__(self, database_path: str | Path) -> None:
        self.data_source = ReadOnlyManualChatGPTDataSource(database_path)

    def build(
        self,
        *,
        as_of_ms: int | None = None,
        timezone_name: str = "Asia/Shanghai",
        waiting_expiry_hours: int = 24,
        max_items: int = 50,
    ) -> dict[str, Any]:
        if as_of_ms is not None and (isinstance(as_of_ms, bool) or not isinstance(as_of_ms, int)):
            raise ManualChatGPTOperationsError(
                "as_of_ms must be an integer.",
                code="OPERATIONS_TIME_INVALID",
            )
        current_ms = int(as_of_ms if as_of_ms is not None else time.time() * 1000)
        if current_ms < 0:
            raise ManualChatGPTOperationsError(
                "as_of_ms must be non-negative.",
                code="OPERATIONS_TIME_INVALID",
            )
        if (
            isinstance(waiting_expiry_hours, bool)
            or not isinstance(waiting_expiry_hours, int)
            or not 1 <= waiting_expiry_hours <= 24 * 30
        ):
            raise ManualChatGPTOperationsError(
                "waiting_expiry_hours must be between 1 and 720.",
                code="OPERATIONS_EXPIRY_INVALID",
            )
        if isinstance(max_items, bool) or not isinstance(max_items, int) or not 1 <= max_items <= MAX_SUMMARY_ITEMS:
            raise ManualChatGPTOperationsError(
                f"max_items must be between 1 and {MAX_SUMMARY_ITEMS}.",
                code="OPERATIONS_LIMIT_INVALID",
            )
        try:
            timezone_value = ZoneInfo(str(timezone_name))
        except ZoneInfoNotFoundError as exc:
            raise ManualChatGPTOperationsError(
                "The requested timezone is unavailable.",
                code="OPERATIONS_TIMEZONE_INVALID",
            ) from exc
        as_of = datetime.fromtimestamp(current_ms / 1000, tz=timezone_value)
        today_start = as_of.replace(hour=0, minute=0, second=0, microsecond=0)
        yesterday_start = today_start - timedelta(days=1)
        yesterday_start_ms = int(yesterday_start.timestamp() * 1000)
        today_start_ms = int(today_start.timestamp() * 1000)
        expiry_cutoff_ms = current_ms - int(waiting_expiry_hours) * 60 * 60 * 1000

        try:
            with closing(self.data_source._connect()) as connection:
                connection.execute("BEGIN")
                session_count = int(connection.execute(
                    "SELECT COUNT(*) FROM manual_chatgpt_sessions"
                ).fetchone()[0])
                if session_count > MAX_SCANNED_SESSIONS:
                    raise ManualChatGPTOperationsError(
                        "The manual-ChatGPT session scan exceeds the report limit.",
                        code="OPERATIONS_SCAN_LIMIT",
                    )
                session_rows = [dict(row) for row in connection.execute(
                    """SELECT * FROM manual_chatgpt_sessions
                         ORDER BY updated_at DESC,id DESC"""
                ).fetchall()]
                event_rows = [dict(row) for row in connection.execute(
                    """SELECT * FROM manual_chatgpt_events
                         ORDER BY session_id,sequence_no"""
                ).fetchall()]
                review_contexts = {
                    str(row.get("id") or ""): self.data_source._read_review_context(
                        connection,
                        session_id=str(row.get("id") or ""),
                        room_id=str(row.get("room_id") or ""),
                    )
                    for row in session_rows
                }
                room_titles = {
                    str(row["id"]): str(row["title"] or "")
                    for row in connection.execute("SELECT id,title FROM rooms").fetchall()
                }
                pending_citations = self._pending_citations(connection, max_items)
                provider_usage = self._yesterday_provider_usage(
                    connection,
                    yesterday_start_ms,
                    today_start_ms,
                )
        except sqlite3.Error as exc:
            raise ManualChatGPTOperationsError(
                "The read-only operations schema is unavailable.",
                code="OPERATIONS_DATABASE_READ_FAILED",
            ) from exc

        events_by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for event in event_rows:
            events_by_session[str(event.get("session_id") or "")].append(event)

        verified: list[dict[str, Any]] = []
        integrity_failures: list[dict[str, Any]] = []
        for row in session_rows:
            try:
                projection = self.data_source._verified_projection(
                    row,
                    events_by_session.get(str(row.get("id") or ""), []),
                    review_context=review_contexts.get(str(row.get("id") or "")),
                )
            except ReadonlyMCPError as exc:
                integrity_failures.append({
                    "room_id": str(row.get("room_id") or ""),
                    "round_id": str(row.get("round_id") or ""),
                    "code": exc.code,
                })
                continue
            verified.append(projection)

        latest_session_id_by_room: dict[str, str] = {}
        for row in session_rows:
            latest_session_id_by_room.setdefault(
                str(row.get("room_id") or ""),
                str(row.get("id") or ""),
            )
        latest_by_room: dict[str, dict[str, Any]] = {}
        for session in verified:
            room_id = str(session["room_id"])
            if str(session["session_id"]) == latest_session_id_by_room.get(room_id):
                latest_by_room[room_id] = session
        incomplete = [
            session for session in latest_by_room.values()
            if str(session["state"]) in INCOMPLETE_STATES
        ]
        waiting = [session for session in verified if session["state"] == "WAITING_FOR_CHATGPT"]
        context_stale = [session for session in verified if session["state"] == "CONTEXT_STALE"]
        operationally_expired = [
            session for session in verified
            if session["state"] in INCOMPLETE_STATES
            and int(session["updated_at"]) < expiry_cutoff_ms
        ]
        planned_cost = self._yesterday_manual_planned_cost(
            verified,
            yesterday_start_ms,
            today_start_ms,
        )

        def items(sessions: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
            return [
                _summary_item(
                    session,
                    room_titles.get(str(session.get("room_id") or ""), ""),
                    current_ms,
                )
                for session in sessions[:max_items]
            ]

        report: dict[str, Any] = {
            "version": OPERATIONS_SUMMARY_VERSION,
            "as_of_ms": current_ms,
            "timezone": str(timezone_name),
            "reporting_window": {
                "yesterday_start_ms": yesterday_start_ms,
                "today_start_ms": today_start_ms,
                "waiting_expiry_hours": waiting_expiry_hours,
                "expiry_is_operational_age_only": True,
            },
            "manual_chatgpt": {
                "source_session_rows": session_count,
                "integrity_verified_sessions": len(verified),
                "integrity_failed_sessions": len(integrity_failures),
                "integrity_failures": integrity_failures[:max_items],
                "incomplete_latest_rooms": {
                    "count": len(incomplete),
                    "items": items(incomplete),
                    "truncated": len(incomplete) > max_items,
                },
                "waiting_for_chatgpt": {
                    "count": len(waiting),
                    "items": items(waiting),
                    "truncated": len(waiting) > max_items,
                },
                "context_stale": {
                    "count": len(context_stale),
                    "items": items(context_stale),
                    "truncated": len(context_stale) > max_items,
                },
                "operationally_age_expired": {
                    "count": len(operationally_expired),
                    "items": items(operationally_expired),
                    "truncated": len(operationally_expired) > max_items,
                    "does_not_change_persisted_state": True,
                },
            },
            "pending_citation_verification": pending_citations,
            "yesterday_provider_usage": provider_usage,
            "yesterday_manual_api_plan_estimate": planned_cost,
            "automation_boundary": {
                "report_only": True,
                "database_write_capability": False,
                "provider_calls_performed": 0,
                "market_calls_performed": 0,
                "result_imports_performed": 0,
                "formal_migration_performed": False,
            },
        }
        report["report_sha256"] = canonical_sha256(report)
        return report

    @staticmethod
    def _pending_citations(connection: Any, max_items: int) -> dict[str, Any]:
        statuses = ("unreviewed", "disputed")
        rows = connection.execute(
            """SELECT a.room_id,ae.artifact_id,ae.item_key,ae.source_type,
                      ae.source_id,ae.verification_status
                 FROM artifact_evidence ae
                 JOIN artifacts a ON a.id=ae.artifact_id
                WHERE ae.verification_status IN (?,?)
                ORDER BY ae.created_at ASC,ae.artifact_id,ae.item_key,
                         ae.source_type,ae.source_id""",
            statuses,
        ).fetchall()
        counts = Counter(str(row["verification_status"] or "unreviewed") for row in rows)
        details = [{
            "room_id": str(row["room_id"] or ""),
            "artifact_id": str(row["artifact_id"] or ""),
            "item_key": str(row["item_key"] or ""),
            "source_type": str(row["source_type"] or ""),
            "source_id": str(row["source_id"] or ""),
            "verification_status": str(row["verification_status"] or "unreviewed"),
        } for row in rows[:max_items]]
        return {
            "count": len(rows),
            "by_status": {key: counts[key] for key in sorted(counts)},
            "items": details,
            "truncated": len(rows) > max_items,
            "relation_chain_integrity_revalidated": False,
            "purpose": "operational_reminder_only",
        }

    @staticmethod
    def _yesterday_provider_usage(
        connection: Any,
        start_ms: int,
        end_ms: int,
    ) -> dict[str, Any]:
        count = int(connection.execute(
            """SELECT COUNT(*) FROM provider_call_attempts
                 WHERE started_at>=? AND started_at<?""",
            (start_ms, end_ms),
        ).fetchone()[0])
        if count > MAX_SCANNED_PROVIDER_CALLS:
            raise ManualChatGPTOperationsError(
                "The provider usage scan exceeds the report limit.",
                code="OPERATIONS_PROVIDER_SCAN_LIMIT",
            )
        rows = connection.execute(
            """SELECT status,elapsed_ms,usage_json,usage_sha256
                 FROM provider_call_attempts
                WHERE started_at>=? AND started_at<?
                ORDER BY started_at,id""",
            (start_ms, end_ms),
        ).fetchall()
        status_counts: Counter[str] = Counter()
        input_tokens = Decimal(0)
        output_tokens = Decimal(0)
        elapsed_ms = 0
        cost_usd = Decimal(0)
        untyped_cost = Decimal(0)
        cost_usd_calls = 0
        untyped_cost_calls = 0
        usage_integrity_failures = 0
        for row in rows:
            status_counts[str(row["status"] or "UNKNOWN")] += 1
            elapsed_ms += max(0, int(row["elapsed_ms"] or 0))
            usage = _json_object(row["usage_json"])
            if canonical_sha256(usage) != str(row["usage_sha256"] or ""):
                usage_integrity_failures += 1
                continue
            input_value = _finite_decimal(usage.get("input_tokens"))
            output_value = _finite_decimal(usage.get("output_tokens"))
            if input_value is not None:
                input_tokens += input_value
            if output_value is not None:
                output_tokens += output_value
            usd_value = _finite_decimal(usage.get("cost_usd"))
            raw_cost = _finite_decimal(usage.get("cost"))
            if usd_value is not None:
                cost_usd += usd_value
                cost_usd_calls += 1
            if raw_cost is not None:
                untyped_cost += raw_cost
                untyped_cost_calls += 1
        return {
            "call_count": count,
            "by_status": {key: status_counts[key] for key in sorted(status_counts)},
            "elapsed_ms_total": elapsed_ms,
            "input_tokens_recorded": int(input_tokens),
            "output_tokens_recorded": int(output_tokens),
            "usage_integrity_failures": usage_integrity_failures,
            "recorded_cost_usd": {
                "status": "available" if cost_usd_calls else "unavailable",
                "amount_usd": _decimal_output(cost_usd) if cost_usd_calls else None,
                "calls_with_value": cost_usd_calls,
            },
            "recorded_untyped_cost": {
                "status": "available_unit_unknown" if untyped_cost_calls else "unavailable",
                "amount": _decimal_output(untyped_cost) if untyped_cost_calls else None,
                "calls_with_value": untyped_cost_calls,
                "must_not_be_presented_as_usd": True,
            },
            "missing_cost_is_not_zero": True,
        }

    @staticmethod
    def _yesterday_manual_planned_cost(
        sessions: Sequence[Mapping[str, Any]],
        start_ms: int,
        end_ms: int,
    ) -> dict[str, Any]:
        amount = Decimal(0)
        estimated_sessions = 0
        unavailable_sessions = 0
        for session in sessions:
            created_at = int(session.get("created_at") or 0)
            if not start_ms <= created_at < end_ms:
                continue
            bundle = session.get("bundle") if isinstance(session.get("bundle"), Mapping) else {}
            planning = bundle.get("planning") if isinstance(bundle.get("planning"), Mapping) else {}
            estimate = (
                planning.get("estimated_api_cost")
                if isinstance(planning.get("estimated_api_cost"), Mapping)
                else {}
            )
            value = _finite_decimal(estimate.get("amount_usd"))
            if estimate.get("status") == "estimated" and value is not None:
                amount += value
                estimated_sessions += 1
            else:
                unavailable_sessions += 1
        return {
            "status": "partially_available" if estimated_sessions and unavailable_sessions else (
                "available" if estimated_sessions else "unavailable"
            ),
            "estimated_amount_usd": _decimal_output(amount) if estimated_sessions else None,
            "sessions_with_estimate": estimated_sessions,
            "sessions_without_estimate": unavailable_sessions,
            "scope": "planned_independent_api_reviews_only",
            "manual_chatgpt_subscription_excluded": True,
            "not_actual_spend": True,
        }


def _markdown_escape(value: Any, limit: int = 240) -> str:
    clean = _bounded_text(sanitize_gateway_value(str(value or "")), limit)
    clean = " ".join(clean.replace("\r", " ").replace("\n", " ").split())
    for character in ("\\", "`", "*", "_", "[", "]", "<", ">", "|", "#"):
        clean = clean.replace(character, f"\\{character}")
    return clean or "未命名"


def _report_mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ManualChatGPTOperationsError(
            f"{path} must be an object.",
            code="OPERATIONS_REPORT_INVALID",
        )
    return value


def _report_count(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ManualChatGPTOperationsError(
            f"{path} must be a non-negative integer.",
            code="OPERATIONS_REPORT_INVALID",
        )
    return value


def _markdown_session_section(title: str, bucket: Mapping[str, Any]) -> list[str]:
    count = _report_count(bucket.get("count"), f"{title}.count")
    items = bucket.get("items")
    if not isinstance(items, list):
        raise ManualChatGPTOperationsError(
            f"{title}.items must be an array.",
            code="OPERATIONS_REPORT_INVALID",
        )
    lines = [f"### {title}（{count}）", ""]
    if not items:
        return lines + ["- 无", ""]
    for raw_item in items:
        item = _report_mapping(raw_item, f"{title}.items[]")
        age_minutes = _report_count(item.get("age_minutes"), f"{title}.age_minutes")
        age_label = (
            f"{age_minutes} 分钟"
            if age_minutes < 60
            else f"{age_minutes // 60} 小时 {age_minutes % 60} 分钟"
        )
        lines.append(
            "- "
            f"{_markdown_escape(item.get('room_title') or item.get('room_id'))}"
            f" — {_markdown_escape(item.get('state'), 80)}"
            f"；{_markdown_escape(item.get('mode'), 40)} 模式"
            f"；距上次更新 {age_label}"
            f"；room={_markdown_escape(item.get('room_id'), 120)}"
            f"，round={_markdown_escape(item.get('round_id'), 120)}"
        )
    if bucket.get("truncated") is True:
        lines.append("- 列表已按 `--max-items` 截断；总数以上方计数为准。")
    return lines + [""]


def render_daily_operations_markdown(report: Mapping[str, Any]) -> str:
    """Render a sealed daily report for a human-facing scheduled reminder."""

    source = dict(_report_mapping(report, "report"))
    if source.get("version") != OPERATIONS_SUMMARY_VERSION:
        raise ManualChatGPTOperationsError(
            "The operations report version is unsupported.",
            code="OPERATIONS_REPORT_INVALID",
        )
    report_sha256 = str(source.pop("report_sha256", ""))
    if not re_full_sha256(report_sha256) or canonical_sha256(source) != report_sha256:
        raise ManualChatGPTOperationsError(
            "The operations report integrity seal is invalid.",
            code="OPERATIONS_REPORT_INVALID",
        )
    boundary = _report_mapping(source.get("automation_boundary"), "automation_boundary")
    expected_boundary = {
        "report_only": True,
        "database_write_capability": False,
        "provider_calls_performed": 0,
        "market_calls_performed": 0,
        "result_imports_performed": 0,
        "formal_migration_performed": False,
    }
    if any(boundary.get(key) != value for key, value in expected_boundary.items()):
        raise ManualChatGPTOperationsError(
            "The operations report does not preserve the read-only automation boundary.",
            code="OPERATIONS_REPORT_INVALID",
        )
    manual = _report_mapping(source.get("manual_chatgpt"), "manual_chatgpt")
    incomplete = _report_mapping(
        manual.get("incomplete_latest_rooms"),
        "manual_chatgpt.incomplete_latest_rooms",
    )
    waiting = _report_mapping(
        manual.get("waiting_for_chatgpt"),
        "manual_chatgpt.waiting_for_chatgpt",
    )
    stale = _report_mapping(manual.get("context_stale"), "manual_chatgpt.context_stale")
    expired = _report_mapping(
        manual.get("operationally_age_expired"),
        "manual_chatgpt.operationally_age_expired",
    )
    citations = _report_mapping(
        source.get("pending_citation_verification"),
        "pending_citation_verification",
    )
    usage = _report_mapping(source.get("yesterday_provider_usage"), "yesterday_provider_usage")
    actual_cost = _report_mapping(usage.get("recorded_cost_usd"), "recorded_cost_usd")
    untyped_cost = _report_mapping(usage.get("recorded_untyped_cost"), "recorded_untyped_cost")
    planned_cost = _report_mapping(
        source.get("yesterday_manual_api_plan_estimate"),
        "yesterday_manual_api_plan_estimate",
    )
    try:
        timezone_value = ZoneInfo(str(source.get("timezone") or ""))
        as_of = datetime.fromtimestamp(int(source.get("as_of_ms")) / 1000, tz=timezone_value)
    except (TypeError, ValueError, OverflowError, ZoneInfoNotFoundError) as exc:
        raise ManualChatGPTOperationsError(
            "The operations report timestamp is invalid.",
            code="OPERATIONS_REPORT_INVALID",
        ) from exc

    counts = {
        "未完成房间": _report_count(incomplete.get("count"), "incomplete.count"),
        "等待 ChatGPT": _report_count(waiting.get("count"), "waiting.count"),
        "上下文已变化": _report_count(stale.get("count"), "stale.count"),
        "运营年龄提醒": _report_count(expired.get("count"), "expired.count"),
        "待核验引用": _report_count(citations.get("count"), "citations.count"),
        "完整性失败": _report_count(
            manual.get("integrity_failed_sessions"),
            "manual_chatgpt.integrity_failed_sessions",
        ),
    }
    lines = [
        "# AI 共创室运营摘要",
        "",
        f"生成时间：{as_of.strftime('%Y-%m-%d %H:%M:%S')} {_markdown_escape(source.get('timezone'), 80)}",
        "",
        "> 只读运营报告：未导入结果、未调用 Provider 或市场接口、未修改数据库、未执行正式迁移。",
        "",
        "## 今日待办概览",
        "",
        "| 项目 | 数量 |",
        "| --- | ---: |",
        *[f"| {label} | {value} |" for label, value in counts.items()],
        "",
    ]
    lines.extend(_markdown_session_section("等待 ChatGPT", waiting))
    lines.extend(_markdown_session_section("上下文已变化", stale))
    lines.extend(_markdown_session_section("运营年龄提醒", expired))

    citation_items = citations.get("items")
    if not isinstance(citation_items, list):
        raise ManualChatGPTOperationsError(
            "pending_citation_verification.items must be an array.",
            code="OPERATIONS_REPORT_INVALID",
        )
    lines.extend([f"### 待核验引用（{counts['待核验引用']}）", ""])
    if not citation_items:
        lines.extend(["- 无", ""])
    else:
        for raw_item in citation_items:
            item = _report_mapping(raw_item, "pending_citation_verification.items[]")
            lines.append(
                "- "
                f"{_markdown_escape(item.get('verification_status'), 40)}"
                f"；room={_markdown_escape(item.get('room_id'), 120)}"
                f"；artifact={_markdown_escape(item.get('artifact_id'), 120)}"
                f"；item={_markdown_escape(item.get('item_key'), 120)}"
            )
        if citations.get("truncated") is True:
            lines.append("- 引用列表已按 `--max-items` 截断。")
        lines.append("")

    status_counts = usage.get("by_status")
    if not isinstance(status_counts, Mapping):
        raise ManualChatGPTOperationsError(
            "yesterday_provider_usage.by_status must be an object.",
            code="OPERATIONS_REPORT_INVALID",
        )
    status_text = "、".join(
        f"{_markdown_escape(key, 60)}={_report_count(value, 'usage.by_status')}"
        for key, value in sorted(status_counts.items(), key=lambda item: str(item[0]))
    ) or "无"
    actual_cost_text = (
        f"USD {actual_cost.get('amount_usd')}"
        if actual_cost.get("status") == "available"
        else "不可用（缺失不按 0 处理）"
    )
    planned_cost_text = (
        f"USD {planned_cost.get('estimated_amount_usd')}"
        if planned_cost.get("status") in {"available", "partially_available"}
        else "不可用"
    )
    lines.extend([
        "## 昨日 API 记录",
        "",
        f"- 调用数：{_report_count(usage.get('call_count'), 'usage.call_count')}（{status_text}）",
        f"- 已记录 Token：输入 {_report_count(usage.get('input_tokens_recorded'), 'usage.input_tokens')}，输出 {_report_count(usage.get('output_tokens_recorded'), 'usage.output_tokens')}。",
        f"- 已记录耗时：{_report_count(usage.get('elapsed_ms_total'), 'usage.elapsed_ms_total')} ms。",
        f"- 已记录美元费用：{actual_cost_text}。",
        (
            f"- 旧版无币种费用：{untyped_cost.get('amount')}（单位未知，禁止作为美元）。"
            if untyped_cost.get("status") == "available_unit_unknown"
            else "- 旧版无币种费用：不可用。"
        ),
        f"- Manual ChatGPT 独立审查计划估算：{planned_cost_text}（非实付，不含 ChatGPT 订阅）。",
        "",
        "## 边界与核验",
        "",
        "- 运营年龄提醒不会改变持久化状态，也不等于上下文已经失效。",
        "- 待核验引用仅是运营提醒，本报告没有重放完整关系链。",
        f"- 使用量完整性失败：{_report_count(usage.get('usage_integrity_failures'), 'usage.usage_integrity_failures')}。",
        f"- 报告 SHA-256：{report_sha256}",
        "",
    ])
    return "\n".join(lines)


def _validated_number(value: Any, path: str, *, integer: bool = False) -> int | float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ManualChatGPTOperationsError(
            f"{path} must be a finite non-negative number or null.",
            code="AB_DATASET_INVALID",
        )
    if value < 0 or value > 1_000_000_000_000_000:
        raise ManualChatGPTOperationsError(
            f"{path} is outside the supported range.",
            code="AB_DATASET_INVALID",
        )
    if integer and not isinstance(value, int):
        raise ManualChatGPTOperationsError(
            f"{path} must be an integer or null.",
            code="AB_DATASET_INVALID",
        )
    return int(value) if integer else float(value)


def _validate_ab_arm(raw: Any, path: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ManualChatGPTOperationsError(
            f"{path} must be an object.",
            code="AB_DATASET_INVALID",
        )
    allowed = set(_AB_NUMERIC_METRICS) | {
        "citation_refs_total",
        "citation_refs_passed",
        "final_conclusion_id",
        "basis",
    }
    unexpected = set(raw) - allowed
    if unexpected:
        raise ManualChatGPTOperationsError(
            f"{path} contains unexpected field {sorted(unexpected)[0]}.",
            code="AB_DATASET_INVALID",
        )
    arm: dict[str, Any] = {}
    for metric in _AB_NUMERIC_METRICS:
        arm[metric] = _validated_number(
            raw.get(metric),
            f"{path}.{metric}",
            integer=metric in {"model_calls", "input_characters", "estimated_tokens", "wait_ms"},
        )
    total = _validated_number(raw.get("citation_refs_total"), f"{path}.citation_refs_total", integer=True)
    passed = _validated_number(raw.get("citation_refs_passed"), f"{path}.citation_refs_passed", integer=True)
    if (total is None) != (passed is None) or (total is not None and passed is not None and passed > total):
        raise ManualChatGPTOperationsError(
            f"{path} citation counts are inconsistent.",
            code="AB_DATASET_INVALID",
        )
    arm["citation_refs_total"] = total
    arm["citation_refs_passed"] = passed
    conclusion = _bounded_text(raw.get("final_conclusion_id"), 160)
    arm["final_conclusion_id"] = conclusion or None
    basis = raw.get("basis")
    if not isinstance(basis, Mapping):
        raise ManualChatGPTOperationsError(
            f"{path}.basis must be an object.",
            code="AB_DATASET_INVALID",
        )
    required_basis = set(_AB_NUMERIC_METRICS) | {"citations", "final_conclusion"}
    if set(basis) != required_basis:
        raise ManualChatGPTOperationsError(
            f"{path}.basis must define every metric exactly once.",
            code="AB_DATASET_INVALID",
        )
    clean_basis: dict[str, str] = {}
    for metric in sorted(required_basis):
        basis_value = str(basis.get(metric) or "")
        if basis_value not in _AB_BASIS_VALUES:
            raise ManualChatGPTOperationsError(
                f"{path}.basis.{metric} is invalid.",
                code="AB_DATASET_INVALID",
            )
        measured_value = (
            arm[metric]
            if metric in arm
            else (
                arm["citation_refs_total"]
                if metric == "citations"
                else arm["final_conclusion_id"]
            )
        )
        if (measured_value is None) != (basis_value == "unavailable"):
            raise ManualChatGPTOperationsError(
                f"{path}.basis.{metric} does not match value availability.",
                code="AB_DATASET_INVALID",
            )
        clean_basis[metric] = basis_value
    arm["basis"] = clean_basis
    return arm


def validate_ab_dataset(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or raw.get("version") != AB_REPLAY_DATASET_VERSION:
        raise ManualChatGPTOperationsError(
            "The A/B replay dataset version is invalid.",
            code="AB_DATASET_INVALID",
        )
    if set(raw) - {"version", "dataset_id", "cases", "targets"}:
        raise ManualChatGPTOperationsError(
            "The A/B replay dataset contains unexpected fields.",
            code="AB_DATASET_INVALID",
        )
    dataset_id = _bounded_text(raw.get("dataset_id"), 120)
    cases = raw.get("cases")
    if not dataset_id or not isinstance(cases, list) or not MIN_AB_CASES <= len(cases) <= MAX_AB_CASES:
        raise ManualChatGPTOperationsError(
            f"A/B replay requires {MIN_AB_CASES} to {MAX_AB_CASES} cases.",
            code="AB_CASE_COUNT_INVALID",
        )
    clean_cases = []
    seen_ids: set[str] = set()
    for index, raw_case in enumerate(cases):
        path = f"$.cases[{index}]"
        if not isinstance(raw_case, Mapping):
            raise ManualChatGPTOperationsError(
                f"{path} must be an object.",
                code="AB_DATASET_INVALID",
            )
        if set(raw_case) - {
            "case_id", "room_id", "round_id", "declared_source_kind",
            "source_snapshot_sha256", "a", "b",
        }:
            raise ManualChatGPTOperationsError(
                f"{path} contains unexpected fields.",
                code="AB_DATASET_INVALID",
            )
        case_id = _bounded_text(raw_case.get("case_id"), 120)
        if not case_id or case_id in seen_ids:
            raise ManualChatGPTOperationsError(
                f"{path}.case_id is missing or duplicated.",
                code="AB_DATASET_INVALID",
            )
        seen_ids.add(case_id)
        source_kind = str(raw_case.get("declared_source_kind") or "")
        if source_kind not in {"historical_round", "synthetic_contract_fixture"}:
            raise ManualChatGPTOperationsError(
                f"{path}.declared_source_kind is invalid.",
                code="AB_DATASET_INVALID",
            )
        source_hash = str(raw_case.get("source_snapshot_sha256") or "").lower()
        if source_kind == "historical_round" and not re_full_sha256(source_hash):
            raise ManualChatGPTOperationsError(
                f"{path}.source_snapshot_sha256 is required for historical rows.",
                code="AB_DATASET_INVALID",
            )
        case = {
            "case_id": case_id,
            "room_id": _bounded_text(raw_case.get("room_id"), 80),
            "round_id": _bounded_text(raw_case.get("round_id"), 80),
            "declared_source_kind": source_kind,
            "source_snapshot_sha256": source_hash,
            "a": _validate_ab_arm(raw_case.get("a"), f"{path}.a"),
            "b": _validate_ab_arm(raw_case.get("b"), f"{path}.b"),
        }
        case["case_sha256"] = canonical_sha256(case)
        clean_cases.append(case)
    targets = raw.get("targets", {})
    if not isinstance(targets, Mapping):
        raise ManualChatGPTOperationsError(
            "$.targets must be an object.",
            code="AB_DATASET_INVALID",
        )
    allowed_targets = {
        "model_calls_reduction_pct_min",
        "input_characters_reduction_pct_min",
        "estimated_tokens_reduction_pct_min",
        "api_cost_usd_reduction_pct_min",
        "wait_ms_reduction_pct_min",
        "human_operation_minutes_reduction_pct_min",
        "citation_pass_rate_delta_points_min",
        "final_conclusion_change_rate_pct_max",
    }
    if set(targets) - allowed_targets:
        raise ManualChatGPTOperationsError(
            "$.targets contains an unsupported target.",
            code="AB_DATASET_INVALID",
        )
    clean_targets: dict[str, float] = {}
    for key, value in targets.items():
        target = _validated_number(value, f"$.targets.{key}")
        if target is None or not 0 <= float(target) <= 100:
            raise ManualChatGPTOperationsError(
                f"$.targets.{key} must be between 0 and 100.",
                code="AB_DATASET_INVALID",
            )
        clean_targets[key] = float(target)
    return {
        "version": AB_REPLAY_DATASET_VERSION,
        "dataset_id": dataset_id,
        "cases": clean_cases,
        "targets": clean_targets,
    }


def re_full_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _metric_aggregate(cases: Sequence[Mapping[str, Any]], metric: str) -> dict[str, Any]:
    comparable = [
        case for case in cases
        if case["a"].get(metric) is not None and case["b"].get(metric) is not None
    ]
    if not comparable:
        return {
            "comparable_cases": 0,
            "coverage_rate": 0.0,
            "a_total": None,
            "b_total": None,
            "delta": None,
            "reduction_pct": None,
            "basis_counts": {"a": {}, "b": {}},
        }
    a_total = sum(Decimal(str(case["a"][metric])) for case in comparable)
    b_total = sum(Decimal(str(case["b"][metric])) for case in comparable)
    reduction = ((a_total - b_total) / a_total * Decimal(100)) if a_total else None
    basis_counts = {
        arm: dict(sorted(Counter(str(case[arm]["basis"][metric]) for case in comparable).items()))
        for arm in ("a", "b")
    }
    integer = metric in {"model_calls", "input_characters", "estimated_tokens", "wait_ms"}
    return {
        "comparable_cases": len(comparable),
        "coverage_rate": round(len(comparable) / len(cases), 6),
        "a_total": int(a_total) if integer else _decimal_output(a_total),
        "b_total": int(b_total) if integer else _decimal_output(b_total),
        "delta": int(b_total - a_total) if integer else _decimal_output(b_total - a_total),
        "reduction_pct": _decimal_output(reduction) if reduction is not None else None,
        "basis_counts": basis_counts,
    }


def build_ab_replay_report(raw_dataset: Any) -> dict[str, Any]:
    dataset = validate_ab_dataset(raw_dataset)
    cases = dataset["cases"]
    metrics = {metric: _metric_aggregate(cases, metric) for metric in _AB_NUMERIC_METRICS}

    citation_cases = [
        case for case in cases
        if case["a"]["citation_refs_total"] is not None
        and case["b"]["citation_refs_total"] is not None
    ]
    a_citation_total = sum(int(case["a"]["citation_refs_total"]) for case in citation_cases)
    b_citation_total = sum(int(case["b"]["citation_refs_total"]) for case in citation_cases)
    a_citation_passed = sum(int(case["a"]["citation_refs_passed"]) for case in citation_cases)
    b_citation_passed = sum(int(case["b"]["citation_refs_passed"]) for case in citation_cases)
    a_citation_rate = (a_citation_passed / a_citation_total * 100) if a_citation_total else None
    b_citation_rate = (b_citation_passed / b_citation_total * 100) if b_citation_total else None
    citation_delta = (
        b_citation_rate - a_citation_rate
        if a_citation_rate is not None and b_citation_rate is not None
        else None
    )
    metrics["citation_pass_rate"] = {
        "comparable_cases": len(citation_cases),
        "coverage_rate": round(len(citation_cases) / len(cases), 6),
        "a_passed": a_citation_passed,
        "a_total": a_citation_total,
        "a_rate_pct": round(a_citation_rate, 6) if a_citation_rate is not None else None,
        "b_passed": b_citation_passed,
        "b_total": b_citation_total,
        "b_rate_pct": round(b_citation_rate, 6) if b_citation_rate is not None else None,
        "delta_points": round(citation_delta, 6) if citation_delta is not None else None,
    }

    conclusion_cases = [
        case for case in cases
        if case["a"]["final_conclusion_id"] is not None
        and case["b"]["final_conclusion_id"] is not None
    ]
    changed = sum(
        case["a"]["final_conclusion_id"] != case["b"]["final_conclusion_id"]
        for case in conclusion_cases
    )
    metrics["final_conclusion_change_rate"] = {
        "comparable_cases": len(conclusion_cases),
        "coverage_rate": round(len(conclusion_cases) / len(cases), 6),
        "changed_cases": changed,
        "rate_pct": round(changed / len(conclusion_cases) * 100, 6) if conclusion_cases else None,
        "role": "quality_guardrail_not_success_metric",
    }

    target_values = dataset["targets"]
    observed_by_target = {
        "model_calls_reduction_pct_min": metrics["model_calls"]["reduction_pct"],
        "input_characters_reduction_pct_min": metrics["input_characters"]["reduction_pct"],
        "estimated_tokens_reduction_pct_min": metrics["estimated_tokens"]["reduction_pct"],
        "api_cost_usd_reduction_pct_min": metrics["api_cost_usd"]["reduction_pct"],
        "wait_ms_reduction_pct_min": metrics["wait_ms"]["reduction_pct"],
        "human_operation_minutes_reduction_pct_min": metrics["human_operation_minutes"]["reduction_pct"],
        "citation_pass_rate_delta_points_min": metrics["citation_pass_rate"]["delta_points"],
        "final_conclusion_change_rate_pct_max": metrics["final_conclusion_change_rate"]["rate_pct"],
    }
    target_evaluation: dict[str, Any] = {}
    for key, target in target_values.items():
        observed = observed_by_target[key]
        maximum = key.endswith("_max")
        target_evaluation[key] = {
            "target": target,
            "observed": observed,
            "status": "unavailable" if observed is None else (
                "met" if (float(observed) <= target if maximum else float(observed) >= target) else "not_met"
            ),
        }

    all_historical = all(case["declared_source_kind"] == "historical_round" for case in cases)
    report: dict[str, Any] = {
        "version": AB_REPLAY_REPORT_VERSION,
        "dataset_id": dataset["dataset_id"],
        "dataset_sha256": canonical_sha256(dataset),
        "case_count": len(cases),
        "case_sha256s": [case["case_sha256"] for case in cases],
        "evidence_class": "declared_historical_replay" if all_historical else "contract_fixture_only",
        "metric_roles": {
            "primary": ["human_operation_minutes", "model_calls", "api_cost_usd"],
            "drivers": ["input_characters", "estimated_tokens", "wait_ms"],
            "guardrails": ["citation_pass_rate", "final_conclusion_change_rate"],
        },
        "metrics": metrics,
        "targets": {
            "provided": bool(target_values),
            "values": target_values,
            "evaluation": target_evaluation,
            "no_default_target_was_assumed": True,
        },
        "verification_boundary": {
            "structure_and_local_hashes_verified": True,
            "declared_historical_source_truth_verified": False,
            "provider_calls_performed": 0,
            "market_calls_performed": 0,
            "database_writes_performed": 0,
            "synthetic_cases_are_not_historical_evidence": not all_historical,
        },
    }
    report["report_sha256"] = canonical_sha256(report)
    return report


def _validate_historical_ab_source(raw: Any, path: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ManualChatGPTOperationsError(
            f"{path} must contain the exact historical A/B source fields.",
            code="AB_SOURCE_INVALID",
        )
    version = str(raw.get("version") or "")
    if version == AB_SOURCE_SNAPSHOT_VERSION_V1:
        required = {"version", "case_id", "room_id", "round_id", "a", "b"}
        if set(raw) != required:
            raise ManualChatGPTOperationsError(
                f"{path} must contain the exact v1 historical A/B source fields.",
                code="AB_SOURCE_INVALID",
            )
        case_id = _bounded_text(raw.get("case_id"), 120)
        room_id = _bounded_text(raw.get("room_id"), 80)
        round_id = _bounded_text(raw.get("round_id"), 80)
        if not case_id or not room_id or not round_id:
            raise ManualChatGPTOperationsError(
                f"{path} must bind non-empty case, room, and round identifiers.",
                code="AB_SOURCE_INVALID",
            )
        snapshot = {
            "version": AB_SOURCE_SNAPSHOT_VERSION_V1,
            "case_id": case_id,
            "room_id": room_id,
            "round_id": round_id,
            "a": _validate_ab_arm(raw.get("a"), f"{path}.a"),
            "b": _validate_ab_arm(raw.get("b"), f"{path}.b"),
        }
        source = dict(snapshot)
        source["source_snapshot_sha256"] = canonical_sha256(snapshot)
        source["provenance_mode"] = "legacy_shared_round_identity"
        source["a_source"] = {
            "source_kind": "legacy_shared_round_identity",
            "room_id": room_id,
            "round_id": round_id,
            "source_record_sha256": None,
            "human_reviewed": None,
        }
        source["b_source"] = {
            "source_kind": "legacy_shared_round_identity",
            "room_id": room_id,
            "round_id": round_id,
            "session_id": None,
            "source_record_sha256": None,
            "human_reviewed": None,
        }
        return source

    if version not in {
        AB_SOURCE_SNAPSHOT_VERSION_V2,
        AB_SOURCE_SNAPSHOT_VERSION_V3,
    }:
        raise ManualChatGPTOperationsError(
            f"{path}.version is invalid.",
            code="AB_SOURCE_INVALID",
        )
    required = {"version", "case_id", "a_source", "b_source", "a", "b"}
    if set(raw) != required:
        raise ManualChatGPTOperationsError(
            f"{path} must contain the exact dual-arm historical A/B source fields.",
            code="AB_SOURCE_INVALID",
        )

    def clean_identifier(value: Any, field_path: str) -> str:
        clean = str(value or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", clean):
            raise ManualChatGPTOperationsError(
                f"{field_path} must be an ASCII identifier.",
                code="AB_SOURCE_INVALID",
            )
        return clean

    def clean_binding(value: Any, field_path: str, *, arm_name: str) -> dict[str, Any]:
        required_binding_fields = {
            "source_kind", "room_id", "round_id", "source_record_sha256",
            "human_reviewed",
        }
        expected_kind = "legacy_reviewed_arm"
        if arm_name == "b":
            required_binding_fields.add("session_id")
            if version == AB_SOURCE_SNAPSHOT_VERSION_V3:
                required_binding_fields.add("human_operation_record")
            expected_kind = "manual_chatgpt_frozen_export"
        if not isinstance(value, Mapping) or set(value) != required_binding_fields:
            raise ManualChatGPTOperationsError(
                f"{field_path} must contain the exact {arm_name.upper()}-arm source fields.",
                code="AB_SOURCE_INVALID",
            )
        if value.get("source_kind") != expected_kind or value.get("human_reviewed") is not True:
            raise ManualChatGPTOperationsError(
                f"{field_path} must use the expected source kind and explicit review attestation.",
                code="AB_SOURCE_INVALID",
            )
        source_hash = str(value.get("source_record_sha256") or "").lower()
        if not re_full_sha256(source_hash):
            raise ManualChatGPTOperationsError(
                f"{field_path}.source_record_sha256 is invalid.",
                code="AB_SOURCE_INVALID",
            )
        binding = {
            "source_kind": expected_kind,
            "room_id": clean_identifier(value.get("room_id"), f"{field_path}.room_id"),
            "round_id": clean_identifier(value.get("round_id"), f"{field_path}.round_id"),
        }
        if arm_name == "b":
            binding["session_id"] = clean_identifier(
                value.get("session_id"),
                f"{field_path}.session_id",
            )
            if version == AB_SOURCE_SNAPSHOT_VERSION_V3:
                raw_record = value.get("human_operation_record")
                required_record_fields = {
                    "minutes", "basis", "source_kind",
                    "included_in_manual_chatgpt_export", "inferred_from_wall_clock",
                }
                if not isinstance(raw_record, Mapping) or set(raw_record) != required_record_fields:
                    raise ManualChatGPTOperationsError(
                        f"{field_path}.human_operation_record is invalid.",
                        code="AB_SOURCE_INVALID",
                    )
                minutes = _finite_decimal(
                    raw_record.get("minutes"),
                    maximum=Decimal("10080"),
                )
                if (
                    minutes is None
                    or minutes <= 0
                    or raw_record.get("basis") != "recorded"
                    or raw_record.get("source_kind") != "operator_reviewed_timer_or_log"
                    or raw_record.get("included_in_manual_chatgpt_export") is not False
                    or raw_record.get("inferred_from_wall_clock") is not False
                ):
                    raise ManualChatGPTOperationsError(
                        f"{field_path}.human_operation_record must be a reviewed positive timer/log value.",
                        code="AB_SOURCE_INVALID",
                    )
                binding["human_operation_record"] = {
                    "minutes": float(_decimal_output(minutes)),
                    "basis": "recorded",
                    "source_kind": "operator_reviewed_timer_or_log",
                    "included_in_manual_chatgpt_export": False,
                    "inferred_from_wall_clock": False,
                }
        binding["source_record_sha256"] = source_hash
        binding["human_reviewed"] = True
        return binding

    case_id = clean_identifier(raw.get("case_id"), f"{path}.case_id")
    a_source = clean_binding(raw.get("a_source"), f"{path}.a_source", arm_name="a")
    b_source = clean_binding(raw.get("b_source"), f"{path}.b_source", arm_name="b")
    clean_a_arm = _validate_ab_arm(raw.get("a"), f"{path}.a")
    clean_b_arm = _validate_ab_arm(raw.get("b"), f"{path}.b")
    if version == AB_SOURCE_SNAPSHOT_VERSION_V3:
        human_record = b_source["human_operation_record"]
        if (
            clean_a_arm["human_operation_minutes"] is None
            or clean_a_arm["human_operation_minutes"] <= 0
            or clean_a_arm["basis"]["human_operation_minutes"]
            not in {"measured", "recorded"}
        ):
            raise ManualChatGPTOperationsError(
                f"{path}.a human operation time must be a positive measured or recorded value.",
                code="AB_SOURCE_INVALID",
            )
        if (
            clean_b_arm["human_operation_minutes"] != human_record["minutes"]
            or clean_b_arm["basis"]["human_operation_minutes"] != "recorded"
        ):
            raise ManualChatGPTOperationsError(
                f"{path}.b human operation time does not match its reviewed record.",
                code="AB_SOURCE_INVALID",
            )
    snapshot = {
        "version": version,
        "case_id": case_id,
        "a_source": a_source,
        "b_source": b_source,
        "a": clean_a_arm,
        "b": clean_b_arm,
    }
    source = dict(snapshot)
    source["source_snapshot_sha256"] = canonical_sha256(snapshot)
    source["provenance_mode"] = (
        "dual_arm_identity_with_recorded_human_time"
        if version == AB_SOURCE_SNAPSHOT_VERSION_V3
        else "dual_arm_identity"
    )
    source["room_id"] = b_source["room_id"]
    source["round_id"] = b_source["round_id"]
    return source


def _historical_source_snapshot_payload(source: Mapping[str, Any]) -> dict[str, Any]:
    if source.get("version") == AB_SOURCE_SNAPSHOT_VERSION_V1:
        fields = ("version", "case_id", "room_id", "round_id", "a", "b")
    else:
        fields = ("version", "case_id", "a_source", "b_source", "a", "b")
    return {field: source[field] for field in fields}


def _read_reviewed_baseline_arm(path: str | Path) -> dict[str, Any]:
    requested = Path(path).expanduser()
    if first_reparse_component(requested) is not None:
        raise ManualChatGPTOperationsError(
            "The reviewed baseline arm path contains a reparse point.",
            code="AB_BASELINE_PATH_UNSAFE",
        )
    clean_path = requested.resolve()
    if not clean_path.is_file():
        raise ManualChatGPTOperationsError(
            "The reviewed baseline arm file does not exist.",
            code="AB_BASELINE_MISSING",
        )
    try:
        before = clean_path.stat()
        if before.st_size > MAX_AB_SOURCE_FILE_BYTES:
            raise ManualChatGPTOperationsError(
                "The reviewed baseline arm file exceeds the size limit.",
                code="AB_BASELINE_TOO_LARGE",
            )
        parsed = _strict_json_loads(clean_path.read_bytes())
        after = clean_path.stat()
    except ManualChatGPTOperationsError:
        raise
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ManualChatGPTOperationsError(
            "The reviewed baseline arm is not valid UTF-8 JSON.",
            code="AB_BASELINE_INVALID",
        ) from exc
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise ManualChatGPTOperationsError(
            "The reviewed baseline arm changed while it was read.",
            code="AB_BASELINE_CHANGED",
        )
    return _validate_ab_arm(parsed, "$.baseline_arm")


def build_historical_ab_source_snapshot_v2(
    database_path: str | Path,
    *,
    case_id: Any,
    baseline_arm_path: str | Path,
    baseline_room_id: Any,
    baseline_round_id: Any,
    room_id: Any,
    round_id: Any,
    acknowledgement: Any,
) -> dict[str, Any]:
    """Compose one reviewed dual-source snapshot without writing it."""

    if acknowledgement != AB_SOURCE_REVIEW_ACKNOWLEDGEMENT:
        raise ManualChatGPTOperationsError(
            "Composing a v2 source requires the exact dual-arm review acknowledgement.",
            code="AB_SOURCE_REVIEW_REQUIRED",
        )
    clean_case_id = _ab_export_identifier(case_id, "case_id")
    clean_baseline_room_id = _ab_export_identifier(baseline_room_id, "baseline_room_id")
    clean_baseline_round_id = _ab_export_identifier(baseline_round_id, "baseline_round_id")
    clean_room_id = _ab_export_identifier(room_id, "room_id")
    clean_round_id = _ab_export_identifier(round_id, "round_id")
    baseline_arm = _read_reviewed_baseline_arm(baseline_arm_path)
    baseline_record = {
        "room_id": clean_baseline_room_id,
        "round_id": clean_baseline_round_id,
        "arm": baseline_arm,
    }
    b_export = build_manual_chatgpt_ab_arm_export(
        database_path,
        room_id=clean_room_id,
        round_id=clean_round_id,
    )
    snapshot = {
        "version": AB_SOURCE_SNAPSHOT_VERSION_V2,
        "case_id": clean_case_id,
        "a_source": {
            "source_kind": "legacy_reviewed_arm",
            "room_id": clean_baseline_room_id,
            "round_id": clean_baseline_round_id,
            "source_record_sha256": canonical_sha256(baseline_record),
            "human_reviewed": True,
        },
        "b_source": {
            "source_kind": "manual_chatgpt_frozen_export",
            "room_id": clean_room_id,
            "round_id": clean_round_id,
            "session_id": str(b_export["session_id"]),
            "source_record_sha256": str(b_export["export_sha256"]),
            "human_reviewed": True,
        },
        "a": baseline_arm,
        "b": b_export["arm"],
    }
    validated = _validate_historical_ab_source(snapshot, "$.source")
    return _historical_source_snapshot_payload(validated)


def build_historical_ab_source_snapshot_v3(
    database_path: str | Path,
    *,
    case_id: Any,
    baseline_arm_path: str | Path,
    baseline_room_id: Any,
    baseline_round_id: Any,
    room_id: Any,
    round_id: Any,
    b_human_operation_minutes: Any,
    acknowledgement: Any,
) -> dict[str, Any]:
    """Compose a dual-source snapshot with reviewed active-human-time input."""

    minutes = _finite_decimal(
        b_human_operation_minutes,
        maximum=Decimal("10080"),
    )
    if minutes is None or minutes <= 0:
        raise ManualChatGPTOperationsError(
            "B-arm human operation minutes must be a positive reviewed timer/log value.",
            code="AB_SOURCE_HUMAN_TIME_INVALID",
        )
    clean_minutes = float(_decimal_output(minutes))
    snapshot = build_historical_ab_source_snapshot_v2(
        database_path,
        case_id=case_id,
        baseline_arm_path=baseline_arm_path,
        baseline_room_id=baseline_room_id,
        baseline_round_id=baseline_round_id,
        room_id=room_id,
        round_id=round_id,
        acknowledgement=acknowledgement,
    )
    snapshot["version"] = AB_SOURCE_SNAPSHOT_VERSION_V3
    snapshot["b_source"] = dict(snapshot["b_source"])
    snapshot["b_source"]["human_operation_record"] = {
        "minutes": clean_minutes,
        "basis": "recorded",
        "source_kind": "operator_reviewed_timer_or_log",
        "included_in_manual_chatgpt_export": False,
        "inferred_from_wall_clock": False,
    }
    snapshot["b"] = dict(snapshot["b"])
    snapshot["b"]["basis"] = dict(snapshot["b"]["basis"])
    snapshot["b"]["human_operation_minutes"] = clean_minutes
    snapshot["b"]["basis"]["human_operation_minutes"] = "recorded"
    validated = _validate_historical_ab_source(snapshot, "$.source")
    return _historical_source_snapshot_payload(validated)


def _read_historical_ab_source_file(
    source_path: Path,
    *,
    source_index: int,
) -> tuple[dict[str, Any], int]:
    try:
        before = source_path.stat()
    except OSError as exc:
        raise ManualChatGPTOperationsError(
            "A historical A/B source file cannot be inspected.",
            code="AB_SOURCE_READ_FAILED",
        ) from exc
    if before.st_size > MAX_AB_SOURCE_FILE_BYTES:
        raise ManualChatGPTOperationsError(
            "A historical A/B source file exceeds the size limit.",
            code="AB_SOURCE_TOO_LARGE",
        )
    try:
        raw_bytes = source_path.read_bytes()
        parsed = _strict_json_loads(raw_bytes)
        after = source_path.stat()
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ManualChatGPTOperationsError(
            "A historical A/B source file is not valid UTF-8 JSON.",
            code="AB_SOURCE_INVALID",
        ) from exc
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise ManualChatGPTOperationsError(
            "A historical A/B source file changed while it was read.",
            code="AB_SOURCE_CHANGED",
        )
    return (
        _validate_historical_ab_source(parsed, f"$.sources[{source_index}]"),
        int(before.st_size),
    )


def _register_historical_source_identity(
    source: Mapping[str, Any],
    *,
    seen_case_ids: set[str],
    seen_arm_rounds: dict[str, set[tuple[str, str]]],
    seen_arm_hashes: dict[str, set[str]],
    seen_b_sessions: set[str],
) -> None:
    case_id = str(source["case_id"])
    arm_rounds: dict[str, tuple[str, str]] = {}
    arm_hashes: dict[str, str] = {}
    for arm_name in ("a", "b"):
        binding = source["a_source"] if arm_name == "a" else source["b_source"]
        arm_rounds[arm_name] = (
            str(binding["room_id"]),
            str(binding["round_id"]),
        )
        source_hash = str(binding.get("source_record_sha256") or "")
        if source_hash:
            arm_hashes[arm_name] = source_hash
    b_session = str(source["b_source"].get("session_id") or "")
    duplicate = case_id in seen_case_ids or any(
        arm_rounds[arm_name] in seen_arm_rounds[arm_name]
        or (
            arm_name in arm_hashes
            and arm_hashes[arm_name] in seen_arm_hashes[arm_name]
        )
        for arm_name in ("a", "b")
    ) or (b_session and b_session in seen_b_sessions)
    if duplicate:
        raise ManualChatGPTOperationsError(
            "Historical A/B sources must use unique case and per-arm source identities.",
            code="AB_SOURCE_DUPLICATE",
        )
    seen_case_ids.add(case_id)
    for arm_name in ("a", "b"):
        seen_arm_rounds[arm_name].add(arm_rounds[arm_name])
        if arm_name in arm_hashes:
            seen_arm_hashes[arm_name].add(arm_hashes[arm_name])
    if b_session:
        seen_b_sessions.add(b_session)


def _read_historical_ab_sources(directory: str | Path) -> list[dict[str, Any]]:
    requested = Path(directory).expanduser()
    if first_reparse_component(requested) is not None:
        raise ManualChatGPTOperationsError(
            "The historical A/B source directory contains a reparse point.",
            code="AB_SOURCE_PATH_UNSAFE",
        )
    clean_directory = requested.resolve()
    if not clean_directory.is_dir():
        raise ManualChatGPTOperationsError(
            "The historical A/B source directory does not exist.",
            code="AB_SOURCE_MISSING",
        )
    try:
        entries = sorted(clean_directory.iterdir(), key=lambda item: item.name.casefold())
    except OSError as exc:
        raise ManualChatGPTOperationsError(
            "The historical A/B source directory cannot be read.",
            code="AB_SOURCE_READ_FAILED",
        ) from exc
    if any(
        entry.suffix.lower() != ".json"
        or not entry.is_file()
        or first_reparse_component(entry) is not None
        for entry in entries
    ):
        raise ManualChatGPTOperationsError(
            "The historical A/B source directory must contain only regular JSON files.",
            code="AB_SOURCE_PATH_UNSAFE",
        )
    if not MIN_AB_CASES <= len(entries) <= MAX_AB_CASES:
        raise ManualChatGPTOperationsError(
            f"Historical A/B replay requires {MIN_AB_CASES} to {MAX_AB_CASES} source files.",
            code="AB_CASE_COUNT_INVALID",
        )
    total_bytes = 0
    sources: list[dict[str, Any]] = []
    seen_case_ids: set[str] = set()
    seen_arm_rounds: dict[str, set[tuple[str, str]]] = {"a": set(), "b": set()}
    seen_arm_hashes: dict[str, set[str]] = {"a": set(), "b": set()}
    seen_b_sessions: set[str] = set()
    for index, source_path in enumerate(entries):
        source, source_bytes = _read_historical_ab_source_file(
            source_path,
            source_index=index,
        )
        total_bytes += source_bytes
        if total_bytes > MAX_DATASET_BYTES:
            raise ManualChatGPTOperationsError(
                "The historical A/B source set exceeds the total size limit.",
                code="AB_SOURCE_TOO_LARGE",
            )
        _register_historical_source_identity(
            source,
            seen_case_ids=seen_case_ids,
            seen_arm_rounds=seen_arm_rounds,
            seen_arm_hashes=seen_arm_hashes,
            seen_b_sessions=seen_b_sessions,
        )
        sources.append(source)
    return sources


def _ab_collection_metric_coverage(
    sources: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    metric_specs = {
        **{metric: (metric, metric) for metric in _AB_NUMERIC_METRICS},
        "citation_pass_rate": ("citation_refs_total", "citations"),
        "final_conclusion_change_rate": ("final_conclusion_id", "final_conclusion"),
    }
    total_cases = len(sources)
    coverage: dict[str, Any] = {}
    incomplete: list[str] = []
    for output_metric, (value_field, basis_field) in metric_specs.items():
        availability: dict[str, list[bool]] = {"a": [], "b": []}
        basis_counts: dict[str, dict[str, int]] = {}
        for arm_name in ("a", "b"):
            arm_availability: list[bool] = []
            arm_basis: Counter[str] = Counter()
            for source in sources:
                arm = source.get(arm_name) if isinstance(source.get(arm_name), Mapping) else {}
                value = arm.get(value_field)
                available = value is not None
                if output_metric == "citation_pass_rate":
                    available = (
                        isinstance(value, int)
                        and not isinstance(value, bool)
                        and value > 0
                        and isinstance(arm.get("citation_refs_passed"), int)
                        and not isinstance(arm.get("citation_refs_passed"), bool)
                    )
                arm_availability.append(available)
                basis = arm.get("basis") if isinstance(arm.get("basis"), Mapping) else {}
                arm_basis[str(basis.get(basis_field) or "unavailable")] += 1
            availability[arm_name] = arm_availability
            basis_counts[arm_name] = dict(sorted(arm_basis.items()))
        comparable = sum(
            a_available and b_available
            for a_available, b_available in zip(availability["a"], availability["b"])
        )
        if comparable != total_cases:
            incomplete.append(output_metric)
        coverage[output_metric] = {
            "a_available_cases": sum(availability["a"]),
            "b_available_cases": sum(availability["b"]),
            "comparable_cases": comparable,
            "coverage_rate": round(comparable / total_cases, 6) if total_cases else 0.0,
            "all_valid_cases_comparable": bool(total_cases) and comparable == total_cases,
            "basis_counts": basis_counts,
        }
    return {
        "valid_case_count": total_cases,
        "metrics": coverage,
        "metrics_with_incomplete_coverage": incomplete,
        "all_metrics_complete": bool(total_cases) and not incomplete,
        "coverage_is_descriptive_not_acceptance_target": True,
        "missing_values_are_not_zero": True,
        "no_default_target_was_assumed": True,
    }


def build_historical_ab_collection_status(
    source_directory: str | Path,
) -> dict[str, Any]:
    """Inspect an in-progress historical snapshot directory without writing it."""

    requested = Path(source_directory).expanduser()
    if first_reparse_component(requested) is not None:
        raise ManualChatGPTOperationsError(
            "The historical A/B source directory contains a reparse point.",
            code="AB_SOURCE_PATH_UNSAFE",
        )
    clean_directory = requested.resolve()
    if not clean_directory.is_dir():
        raise ManualChatGPTOperationsError(
            "The historical A/B source directory does not exist.",
            code="AB_SOURCE_MISSING",
        )
    try:
        entries = sorted(clean_directory.iterdir(), key=lambda item: item.name.casefold())
    except OSError as exc:
        raise ManualChatGPTOperationsError(
            "The historical A/B source directory cannot be read.",
            code="AB_SOURCE_READ_FAILED",
        ) from exc

    total_bytes = 0
    valid_sources: list[dict[str, Any]] = []
    validated_sources: list[dict[str, Any]] = []
    invalid_entries: list[dict[str, str]] = []
    seen_case_ids: set[str] = set()
    seen_arm_rounds: dict[str, set[tuple[str, str]]] = {"a": set(), "b": set()}
    seen_arm_hashes: dict[str, set[str]] = {"a": set(), "b": set()}
    seen_b_sessions: set[str] = set()
    scanned_entries = entries[:MAX_AB_COLLECTION_SCAN_ENTRIES]
    for index, source_path in enumerate(scanned_entries):
        safe_name = _bounded_text(sanitize_gateway_value(source_path.name), 180)
        if (
            source_path.suffix.lower() != ".json"
            or not source_path.is_file()
            or first_reparse_component(source_path) is not None
        ):
            invalid_entries.append({
                "file": safe_name,
                "code": "AB_SOURCE_PATH_UNSAFE",
                "message": "Only regular non-reparse JSON files are accepted.",
            })
            continue
        try:
            source, source_bytes = _read_historical_ab_source_file(
                source_path,
                source_index=index,
            )
            total_bytes += source_bytes
            if total_bytes > MAX_DATASET_BYTES:
                raise ManualChatGPTOperationsError(
                    "The historical A/B source set exceeds the total size limit.",
                    code="AB_SOURCE_TOO_LARGE",
                )
            _register_historical_source_identity(
                source,
                seen_case_ids=seen_case_ids,
                seen_arm_rounds=seen_arm_rounds,
                seen_arm_hashes=seen_arm_hashes,
                seen_b_sessions=seen_b_sessions,
            )
            case_id = str(source["case_id"])
            validated_sources.append(source)
            valid_sources.append({
                "file": safe_name,
                "case_id": case_id,
                "source_version": str(source["version"]),
                "provenance_mode": str(source["provenance_mode"]),
                "room_id": str(source["room_id"]),
                "round_id": str(source["round_id"]),
                "a_source": source["a_source"],
                "b_source": source["b_source"],
                "source_snapshot_sha256": str(source["source_snapshot_sha256"]),
            })
        except ManualChatGPTOperationsError as exc:
            invalid_entries.append({
                "file": safe_name,
                "code": exc.code,
                "message": str(exc),
            })

    valid_count = len(valid_sources)
    entry_count = len(entries)
    ready = (
        not invalid_entries
        and MIN_AB_CASES <= valid_count <= MAX_AB_CASES
        and valid_count == entry_count
    )
    source_version_counts = dict(sorted(Counter(
        str(source["version"]) for source in validated_sources
    ).items()))
    v2_dual_arm_cases = source_version_counts.get(AB_SOURCE_SNAPSHOT_VERSION_V2, 0)
    human_time_bound_cases = source_version_counts.get(AB_SOURCE_SNAPSHOT_VERSION_V3, 0)
    dual_arm_bound_cases = v2_dual_arm_cases + human_time_bound_cases
    legacy_shared_identity_cases = source_version_counts.get(AB_SOURCE_SNAPSHOT_VERSION_V1, 0)
    ready_for_dual_arm_replay = ready and dual_arm_bound_cases == valid_count
    metric_coverage = _ab_collection_metric_coverage(validated_sources)
    ready_for_complete_ab_replay = (
        ready_for_dual_arm_replay
        and human_time_bound_cases == valid_count
        and metric_coverage["all_metrics_complete"]
    )
    if ready:
        collection_state = "ready_for_replay"
    elif entry_count > MAX_AB_CASES:
        collection_state = "over_limit"
    elif invalid_entries:
        collection_state = "invalid"
    else:
        collection_state = "collecting"
    report: dict[str, Any] = {
        "version": AB_COLLECTION_STATUS_VERSION,
        "collection_state": collection_state,
        "ready_for_replay": ready,
        "ready_for_dual_arm_replay": ready_for_dual_arm_replay,
        "ready_for_complete_ab_replay": ready_for_complete_ab_replay,
        "discovered_entries": entry_count,
        "scanned_entries": len(scanned_entries),
        "scan_truncated": entry_count > len(scanned_entries),
        "valid_unique_cases": valid_count,
        "invalid_entries": invalid_entries,
        "remaining_to_minimum": max(0, MIN_AB_CASES - valid_count),
        "capacity_remaining": max(0, MAX_AB_CASES - valid_count),
        "minimum_cases": MIN_AB_CASES,
        "maximum_cases": MAX_AB_CASES,
        "source_version_counts": source_version_counts,
        "dual_arm_bound_cases": dual_arm_bound_cases,
        "human_time_bound_cases": human_time_bound_cases,
        "legacy_shared_identity_cases": legacy_shared_identity_cases,
        "valid_sources": valid_sources,
        "metric_coverage": metric_coverage,
        "verification_boundary": {
            "source_directory_returned": False,
            "source_files_modified": False,
            "database_reads_performed": 0,
            "database_writes_performed": 0,
            "provider_calls_performed": 0,
            "market_calls_performed": 0,
            "dual_arm_source_bindings_structurally_verified": ready_for_dual_arm_replay,
            "reviewed_human_operation_records_present": (
                ready and human_time_bound_cases == valid_count
            ),
            "complete_metric_coverage_verified": ready_for_complete_ab_replay,
            "source_record_contents_verified": False,
            "source_record_hash_recomputation_performed": False,
            "legacy_v1_shared_identity_present": legacy_shared_identity_cases > 0,
            "declared_historical_source_truth_verified": False,
            "ready_means_local_contract_only": True,
        },
    }
    report["report_sha256"] = canonical_sha256(report)
    return report


def build_historical_ab_replay_report(
    source_directory: str | Path,
    *,
    dataset_id: str,
) -> dict[str, Any]:
    clean_dataset_id = _bounded_text(dataset_id, 120)
    if not clean_dataset_id:
        raise ManualChatGPTOperationsError(
            "Historical A/B replay requires a dataset id.",
            code="AB_DATASET_INVALID",
        )
    sources = _read_historical_ab_sources(source_directory)
    dataset = {
        "version": AB_REPLAY_DATASET_VERSION,
        "dataset_id": clean_dataset_id,
        "cases": [{
            "case_id": source["case_id"],
            "room_id": source["room_id"],
            "round_id": source["round_id"],
            "declared_source_kind": "historical_round",
            "source_snapshot_sha256": source["source_snapshot_sha256"],
            "a": source["a"],
            "b": source["b"],
        } for source in sources],
        "targets": {},
    }
    report = build_ab_replay_report(dataset)
    report.pop("report_sha256", None)
    all_dual_arm_bound = all(
        source["version"] in {
            AB_SOURCE_SNAPSHOT_VERSION_V2,
            AB_SOURCE_SNAPSHOT_VERSION_V3,
        }
        for source in sources
    )
    all_human_time_bound = all(
        source["version"] == AB_SOURCE_SNAPSHOT_VERSION_V3
        for source in sources
    )
    metric_coverage = _ab_collection_metric_coverage(sources)
    complete_ab_replay = all_human_time_bound and metric_coverage["all_metrics_complete"]
    report["evidence_class"] = (
        "hash_bound_complete_dual_arm_historical_replay"
        if complete_ab_replay
        else (
            "hash_bound_dual_arm_historical_replay"
            if all_dual_arm_bound
            else "hash_bound_historical_replay"
        )
    )
    report["source_snapshot_sha256s"] = [
        str(source["source_snapshot_sha256"]) for source in sources
    ]
    report["source_bindings"] = [{
        "case_id": str(source["case_id"]),
        "source_version": str(source["version"]),
        "provenance_mode": str(source["provenance_mode"]),
        "a_source": source["a_source"],
        "b_source": source["b_source"],
    } for source in sources]
    report["verification_boundary"].update({
        "local_source_snapshot_contents_verified": True,
        "source_snapshot_case_bindings_verified": True,
        "dual_arm_source_bindings_structurally_verified": all_dual_arm_bound,
        "source_record_contents_verified": False,
        "source_record_hash_recomputation_performed": False,
        "human_review_attestations_present": all_dual_arm_bound,
        "reviewed_human_operation_records_present": all_human_time_bound,
        "complete_metric_coverage_verified": complete_ab_replay,
        "legacy_v1_shared_identity_present": any(
            source["version"] == AB_SOURCE_SNAPSHOT_VERSION_V1
            for source in sources
        ),
        "declared_historical_source_truth_verified": False,
    })
    report["report_sha256"] = canonical_sha256(report)
    return report


def _read_dataset(path: str | Path) -> Any:
    requested = Path(path).expanduser()
    if first_reparse_component(requested) is not None:
        raise ManualChatGPTOperationsError(
            "The A/B dataset path contains a reparse point.",
            code="AB_DATASET_PATH_UNSAFE",
        )
    clean_path = requested.resolve()
    if not clean_path.is_file():
        raise ManualChatGPTOperationsError(
            "The A/B dataset file does not exist.",
            code="AB_DATASET_MISSING",
        )
    before = clean_path.stat()
    if before.st_size > MAX_DATASET_BYTES:
        raise ManualChatGPTOperationsError(
            "The A/B dataset exceeds the size limit.",
            code="AB_DATASET_TOO_LARGE",
        )
    try:
        raw = clean_path.read_bytes()
        parsed = _strict_json_loads(raw)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ManualChatGPTOperationsError(
            "The A/B dataset is not valid UTF-8 JSON.",
            code="AB_DATASET_INVALID",
        ) from exc
    after = clean_path.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise ManualChatGPTOperationsError(
            "The A/B dataset changed while it was read.",
            code="AB_DATASET_CHANGED",
        )
    return parsed


def _parse_as_of(value: str, timezone_name: str) -> int | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ManualChatGPTOperationsError(
            "--as-of must be an ISO-8601 datetime.",
            code="OPERATIONS_TIME_INVALID",
        ) from exc
    return int(parsed.timestamp() * 1000)


def _build_cli() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only manual-ChatGPT operations and A/B replay reports."
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    daily = subcommands.add_parser("daily-summary")
    daily.add_argument("--database", type=Path, required=True)
    daily.add_argument("--as-of", default="")
    daily.add_argument("--timezone", default="Asia/Shanghai")
    daily.add_argument("--waiting-expiry-hours", type=int, default=24)
    daily.add_argument("--max-items", type=int, default=50)
    daily.add_argument(
        "--format",
        dest="output_format",
        choices=("json", "markdown"),
        default="json",
    )
    scheduled_daily = subcommands.add_parser("scheduled-daily-summary")
    scheduled_daily.add_argument("--as-of", default="")
    scheduled_daily.add_argument("--timezone", default="Asia/Shanghai")
    scheduled_daily.add_argument("--waiting-expiry-hours", type=int, default=24)
    scheduled_daily.add_argument("--max-items", type=int, default=50)
    scheduled_daily.add_argument(
        "--format",
        dest="output_format",
        choices=("json", "markdown"),
        default="markdown",
    )
    task_contract = subcommands.add_parser("scheduled-task-contract")
    task_contract.add_argument("--timezone", default="Asia/Shanghai")
    task_contract.add_argument("--local-time", default="09:00")
    task_contract.add_argument("--waiting-expiry-hours", type=int, default=24)
    task_contract.add_argument("--max-items", type=int, default=50)
    replay = subcommands.add_parser("ab-replay")
    replay.add_argument("--dataset", type=Path, required=True)
    historical = subcommands.add_parser("historical-ab-replay")
    historical.add_argument("--source-directory", type=Path, required=True)
    historical.add_argument("--dataset-id", required=True)
    arm_export = subcommands.add_parser("historical-ab-export-b-arm")
    arm_export.add_argument("--database", type=Path, required=True)
    arm_export.add_argument("--room-id", required=True)
    arm_export.add_argument("--round-id", required=True)
    compose = subcommands.add_parser("historical-ab-compose-v2")
    compose.add_argument("--database", type=Path, required=True)
    compose.add_argument("--case-id", required=True)
    compose.add_argument("--baseline-arm", type=Path, required=True)
    compose.add_argument("--baseline-room-id", required=True)
    compose.add_argument("--baseline-round-id", required=True)
    compose.add_argument("--room-id", required=True)
    compose.add_argument("--round-id", required=True)
    compose.add_argument("--acknowledgement", required=True)
    compose_v3 = subcommands.add_parser("historical-ab-compose-v3")
    compose_v3.add_argument("--database", type=Path, required=True)
    compose_v3.add_argument("--case-id", required=True)
    compose_v3.add_argument("--baseline-arm", type=Path, required=True)
    compose_v3.add_argument("--baseline-room-id", required=True)
    compose_v3.add_argument("--baseline-round-id", required=True)
    compose_v3.add_argument("--room-id", required=True)
    compose_v3.add_argument("--round-id", required=True)
    compose_v3.add_argument("--b-human-operation-minutes", type=float, required=True)
    compose_v3.add_argument("--acknowledgement", required=True)
    collection = subcommands.add_parser("historical-ab-status")
    collection.add_argument("--source-directory", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _build_cli().parse_args(argv)
    try:
        if arguments.command in {"daily-summary", "scheduled-daily-summary"}:
            database_path = (
                arguments.database
                if arguments.command == "daily-summary"
                else _scheduled_database_from_environment()
            )
            report = DailyOperationsSummary(database_path).build(
                as_of_ms=_parse_as_of(arguments.as_of, arguments.timezone),
                timezone_name=arguments.timezone,
                waiting_expiry_hours=arguments.waiting_expiry_hours,
                max_items=arguments.max_items,
            )
            if arguments.output_format == "markdown":
                print(render_daily_operations_markdown(report))
                return 0
        elif arguments.command == "scheduled-task-contract":
            report = build_scheduled_task_contract(
                timezone_name=arguments.timezone,
                local_time=arguments.local_time,
                waiting_expiry_hours=arguments.waiting_expiry_hours,
                max_items=arguments.max_items,
            )
        elif arguments.command == "ab-replay":
            report = build_ab_replay_report(_read_dataset(arguments.dataset))
        elif arguments.command == "historical-ab-replay":
            report = build_historical_ab_replay_report(
                arguments.source_directory,
                dataset_id=arguments.dataset_id,
            )
        elif arguments.command == "historical-ab-export-b-arm":
            report = build_manual_chatgpt_ab_arm_export(
                arguments.database,
                room_id=arguments.room_id,
                round_id=arguments.round_id,
            )
        elif arguments.command == "historical-ab-compose-v2":
            report = build_historical_ab_source_snapshot_v2(
                arguments.database,
                case_id=arguments.case_id,
                baseline_arm_path=arguments.baseline_arm,
                baseline_room_id=arguments.baseline_room_id,
                baseline_round_id=arguments.baseline_round_id,
                room_id=arguments.room_id,
                round_id=arguments.round_id,
                acknowledgement=arguments.acknowledgement,
            )
        elif arguments.command == "historical-ab-compose-v3":
            report = build_historical_ab_source_snapshot_v3(
                arguments.database,
                case_id=arguments.case_id,
                baseline_arm_path=arguments.baseline_arm,
                baseline_room_id=arguments.baseline_room_id,
                baseline_round_id=arguments.baseline_round_id,
                room_id=arguments.room_id,
                round_id=arguments.round_id,
                b_human_operation_minutes=arguments.b_human_operation_minutes,
                acknowledgement=arguments.acknowledgement,
            )
        else:
            report = build_historical_ab_collection_status(arguments.source_directory)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False))
        return 0
    except (ManualChatGPTOperationsError, ReadonlyMCPError) as exc:
        print(json.dumps({
            "ok": False,
            "code": getattr(exc, "code", "OPERATIONS_FAILED"),
            "message": str(exc),
        }, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "AB_REPLAY_DATASET_VERSION",
    "AB_REPLAY_REPORT_VERSION",
    "AB_SOURCE_SNAPSHOT_VERSION",
    "AB_SOURCE_SNAPSHOT_VERSION_V1",
    "AB_SOURCE_SNAPSHOT_VERSION_V2",
    "AB_SOURCE_SNAPSHOT_VERSION_V3",
    "AB_SOURCE_REVIEW_ACKNOWLEDGEMENT",
    "AB_COLLECTION_STATUS_VERSION",
    "AB_ARM_EXPORT_VERSION",
    "SCHEDULED_OPERATIONS_DATABASE_ENV",
    "SCHEDULED_TASK_CONTRACT_VERSION",
    "DailyOperationsSummary",
    "ManualChatGPTOperationsError",
    "OPERATIONS_SUMMARY_VERSION",
    "build_scheduled_task_contract",
    "build_manual_chatgpt_ab_arm_export",
    "build_historical_ab_source_snapshot_v2",
    "build_historical_ab_source_snapshot_v3",
    "build_ab_replay_report",
    "build_historical_ab_replay_report",
    "build_historical_ab_collection_status",
    "render_daily_operations_markdown",
    "validate_ab_dataset",
]
