from __future__ import annotations

import copy
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from itertools import count
from pathlib import Path
from unittest.mock import patch

os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"

from backend.decision_lineage import canonical_sha256
from backend.manual_chatgpt import (
    MANUAL_CHATGPT_FREEZE_ACKNOWLEDGEMENT,
    ManualChatGPTService,
)
from backend.manual_chatgpt_operations import (
    AB_ARM_EXPORT_VERSION,
    AB_REPLAY_DATASET_VERSION,
    AB_SOURCE_REVIEW_ACKNOWLEDGEMENT,
    AB_SOURCE_SNAPSHOT_VERSION,
    AB_SOURCE_SNAPSHOT_VERSION_V1,
    AB_SOURCE_SNAPSHOT_VERSION_V2,
    AB_SOURCE_SNAPSHOT_VERSION_V3,
    SCHEDULED_OPERATIONS_DATABASE_ENV,
    SCHEDULED_TASK_CONTRACT_VERSION,
    DailyOperationsSummary,
    ManualChatGPTOperationsError,
    build_ab_replay_report,
    build_historical_ab_collection_status,
    build_historical_ab_replay_report,
    build_historical_ab_source_snapshot_v2,
    build_historical_ab_source_snapshot_v3,
    build_manual_chatgpt_ab_arm_export,
    build_scheduled_task_contract,
    render_daily_operations_markdown,
    validate_ab_dataset,
)
from backend.providers.registry import ProviderRegistry
from backend.store import StudioStore
from tests.test_manual_chatgpt import FakeReviewProvider, valid_result


def replay_arm(
    *,
    model_calls: int,
    input_characters: int,
    estimated_tokens: int,
    api_cost_usd: float,
    wait_ms: int | None,
    human_operation_minutes: float,
    citations_passed: int,
    conclusion: str,
    projected: bool,
) -> dict[str, object]:
    normal_basis = "projected" if projected else "measured"
    return {
        "model_calls": model_calls,
        "input_characters": input_characters,
        "estimated_tokens": estimated_tokens,
        "api_cost_usd": api_cost_usd,
        "wait_ms": wait_ms,
        "human_operation_minutes": human_operation_minutes,
        "citation_refs_total": 10,
        "citation_refs_passed": citations_passed,
        "final_conclusion_id": conclusion,
        "basis": {
            "model_calls": normal_basis,
            "input_characters": normal_basis,
            "estimated_tokens": "projected" if projected else "estimated",
            "api_cost_usd": "estimated" if projected else "recorded",
            "wait_ms": "unavailable" if wait_ms is None else normal_basis,
            "human_operation_minutes": normal_basis,
            "citations": normal_basis,
            "final_conclusion": normal_basis,
        },
    }


def replay_dataset(count: int = 24) -> dict[str, object]:
    cases = []
    for index in range(count):
        cases.append({
            "case_id": f"case_{index + 1:02d}",
            "room_id": f"room_{index + 1:02d}",
            "round_id": f"round_{index + 1:02d}",
            "declared_source_kind": "synthetic_contract_fixture",
            "source_snapshot_sha256": "",
            "a": replay_arm(
                model_calls=12,
                input_characters=12_000,
                estimated_tokens=3_000,
                api_cost_usd=0.12,
                wait_ms=None if index == 0 else 120_000,
                human_operation_minutes=12,
                citations_passed=8,
                conclusion="option_a",
                projected=False,
            ),
            "b": replay_arm(
                model_calls=5,
                input_characters=5_000,
                estimated_tokens=1_250,
                api_cost_usd=0.05,
                wait_ms=None if index == 0 else 60_000,
                human_operation_minutes=5,
                citations_passed=9,
                conclusion="option_b" if index < 6 else "option_a",
                projected=True,
            ),
        })
    return {
        "version": AB_REPLAY_DATASET_VERSION,
        "dataset_id": "synthetic_24_case_contract",
        "cases": cases,
        "targets": {},
    }


def write_historical_sources(
    directory: Path,
    count: int = 24,
    *,
    version: str = AB_SOURCE_SNAPSHOT_VERSION,
) -> None:
    dataset = replay_dataset(count)
    for index, case in enumerate(dataset["cases"], start=1):
        if version == AB_SOURCE_SNAPSHOT_VERSION_V1:
            source = {
                "version": version,
                "case_id": case["case_id"],
                "room_id": case["room_id"],
                "round_id": case["round_id"],
                "a": case["a"],
                "b": case["b"],
            }
        else:
            a_room_id = f"legacy_room_{index:02d}"
            a_round_id = f"legacy_round_{index:02d}"
            b_room_id = str(case["room_id"])
            b_round_id = str(case["round_id"])
            b_arm = copy.deepcopy(case["b"])
            if version == AB_SOURCE_SNAPSHOT_VERSION_V3:
                b_arm["basis"]["human_operation_minutes"] = "recorded"
            source = {
                "version": version,
                "case_id": case["case_id"],
                "a_source": {
                    "source_kind": "legacy_reviewed_arm",
                    "room_id": a_room_id,
                    "round_id": a_round_id,
                    "source_record_sha256": canonical_sha256({
                        "room_id": a_room_id,
                        "round_id": a_round_id,
                        "arm": case["a"],
                    }),
                    "human_reviewed": True,
                },
                "b_source": {
                    "source_kind": "manual_chatgpt_frozen_export",
                    "room_id": b_room_id,
                    "round_id": b_round_id,
                    "session_id": f"manual_session_{index:02d}",
                    "source_record_sha256": canonical_sha256({
                        "room_id": b_room_id,
                        "round_id": b_round_id,
                        "session_id": f"manual_session_{index:02d}",
                        "arm": b_arm,
                    }),
                    "human_reviewed": True,
                },
                "a": case["a"],
                "b": b_arm,
            }
            if version == AB_SOURCE_SNAPSHOT_VERSION_V3:
                source["b_source"]["human_operation_record"] = {
                    "minutes": b_arm["human_operation_minutes"],
                    "basis": "recorded",
                    "source_kind": "operator_reviewed_timer_or_log",
                    "included_in_manual_chatgpt_export": False,
                    "inferred_from_wall_clock": False,
                }
        (directory / f"case-{index:02d}.json").write_text(
            json.dumps(source, ensure_ascii=False),
            encoding="utf-8",
        )


class DailyOperationsSummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        # Noon + 25 hours is both age-expired and on the next Shanghai day.
        # A wall-clock fixture created after 23:00 can skip that reporting day.
        fixture_clock = count(int(datetime.fromisoformat(
            "2026-08-25T12:00:00+08:00",
        ).timestamp() * 1000))
        for target in ("backend.store.now_ms", "backend.manual_chatgpt.now_ms"):
            self.enterContext(patch(target, side_effect=lambda: next(fixture_clock)))
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="ai-studio-operations-",
            ignore_cleanup_errors=True,
        )
        self.database_path = Path(self.temp_dir.name) / "studio.sqlite3"
        self.store = StudioStore(self.database_path)
        room_one = self.store.create_room("Waiting room", "Manual collaboration wait.")
        room_two = self.store.create_room("Stale room", "Manual collaboration stale.")
        self.room_one = str(room_one["room"]["id"])
        self.room_two = str(room_two["room"]["id"])
        rate_card = {
            "label": "isolated-test-rate",
            "input_usd_per_million_tokens": "2",
            "output_usd_per_million_tokens": "8",
        }
        service = ManualChatGPTService(self.store, review_rate_card=rate_card)
        waiting = service.create(
            self.room_one,
            objective="Wait for a manual ChatGPT result.",
            mode="standard",
        )
        self.waiting = service.dispatch(self.room_one, waiting["id"])
        stale = service.create(
            self.room_two,
            objective="Detect a changed evidence context.",
            mode="quick",
        )
        stale = service.dispatch(self.room_two, stale["id"])
        self.store.add_material(self.room_two, {
            "title": "Later evidence",
            "kind": "note",
            "content": "This material was added after the bundle was frozen.",
        })
        self.stale = service.import_result(self.room_two, stale["id"], "{}")
        self.as_of_ms = max(self.waiting["created_at"], self.stale["created_at"]) + 25 * 60 * 60 * 1000
        self._insert_operational_facts()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _insert_operational_facts(self) -> None:
        usage = {"cost_usd": 0.04, "input_tokens": 100, "output_tokens": 50}
        timestamp = int(self.waiting["created_at"])
        with closing(sqlite3.connect(self.database_path)) as connection:
            with connection:
                connection.execute(
                    """INSERT INTO provider_execution_runs(
                           id,room_id,scope,client_request_id,plan_hash,max_calls,
                           reserved_calls,completed_calls,status,skip_policy_json,
                           skip_policy_sha256,created_at,updated_at,completed_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "run_operations", self.room_one, "round", "operations-request",
                        "a" * 64, 1, 1, 1, "COMPLETED", "{}",
                        canonical_sha256({}), timestamp, timestamp, timestamp + 10,
                    ),
                )
                connection.execute(
                    """INSERT INTO provider_call_attempts(
                           id,run_id,sequence_no,kind,provider,status,elapsed_ms,
                           usage_json,usage_sha256,attempt_token_sha256,
                           started_at,finished_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "call_operations", "run_operations", 1, "member_turn",
                        "isolated-provider", "RESPONDED", 250,
                        json.dumps(usage, sort_keys=True, separators=(",", ":")),
                        canonical_sha256(usage), "b" * 64, timestamp, timestamp + 250,
                    ),
                )
                connection.execute(
                    """INSERT INTO artifacts(
                           id,room_id,title,status,content_json,created_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?)""",
                    (
                        "artifact_operations", self.room_one, "Pending evidence",
                        "DRAFT", "{}", timestamp, timestamp,
                    ),
                )
                connection.execute(
                    """INSERT INTO artifact_evidence(
                           artifact_id,item_key,source_type,source_id,
                           verification_status,created_at
                       ) VALUES(?,?,?,?,?,?)""",
                    (
                        "artifact_operations", "item_one", "material",
                        "material_one", "unreviewed", timestamp,
                    ),
                )

    def test_daily_summary_is_read_only_bounded_and_cost_calibrated(self) -> None:
        before = hashlib.sha256(self.database_path.read_bytes()).hexdigest()
        report = DailyOperationsSummary(self.database_path).build(
            as_of_ms=self.as_of_ms,
            waiting_expiry_hours=24,
            max_items=10,
        )
        after = hashlib.sha256(self.database_path.read_bytes()).hexdigest()
        self.assertEqual(after, before)
        self.assertEqual(report["manual_chatgpt"]["waiting_for_chatgpt"]["count"], 1)
        self.assertEqual(report["manual_chatgpt"]["context_stale"]["count"], 1)
        self.assertEqual(report["manual_chatgpt"]["operationally_age_expired"]["count"], 2)
        self.assertTrue(
            report["manual_chatgpt"]["operationally_age_expired"]
            ["does_not_change_persisted_state"]
        )
        self.assertEqual(report["pending_citation_verification"]["count"], 1)
        self.assertFalse(
            report["pending_citation_verification"]["relation_chain_integrity_revalidated"]
        )
        usage = report["yesterday_provider_usage"]
        self.assertEqual(usage["call_count"], 1)
        self.assertEqual(usage["recorded_cost_usd"]["amount_usd"], "0.040000")
        self.assertEqual(usage["recorded_untyped_cost"]["status"], "unavailable")
        plan = report["yesterday_manual_api_plan_estimate"]
        self.assertEqual(plan["status"], "available")
        self.assertTrue(plan["not_actual_spend"])
        self.assertNotEqual(plan["estimated_amount_usd"], "0.000000")
        self.assertEqual(report["automation_boundary"]["provider_calls_performed"], 0)
        report_hash = report.pop("report_sha256")
        self.assertEqual(report_hash, canonical_sha256(report))

    def test_yesterday_usage_follows_local_calendar_not_elapsed_age(self) -> None:
        before = hashlib.sha256(self.database_path.read_bytes()).hexdigest()
        late_evening = datetime.fromisoformat("2026-08-25T23:30:00+08:00")
        usage_created_at = int(self.waiting["created_at"])
        for elapsed_hours, expected_calls in ((23, 1), (25, 0)):
            with self.subTest(elapsed_hours=elapsed_hours):
                as_of = late_evening + timedelta(hours=elapsed_hours)
                report = DailyOperationsSummary(self.database_path).build(
                    as_of_ms=int(as_of.timestamp() * 1000),
                    waiting_expiry_hours=24,
                    max_items=10,
                )
                window = report["reporting_window"]
                in_yesterday = (
                    window["yesterday_start_ms"] <= usage_created_at
                    < window["today_start_ms"]
                )
                self.assertEqual(in_yesterday, bool(expected_calls))
                self.assertEqual(report["yesterday_provider_usage"]["call_count"], expected_calls)
                self.assertEqual(report["manual_chatgpt"]["operationally_age_expired"]["count"], 2)
        self.assertEqual(hashlib.sha256(self.database_path.read_bytes()).hexdigest(), before)

    def test_latest_integrity_failure_does_not_fall_back_to_older_session(self) -> None:
        service = ManualChatGPTService(self.store, review_rate_card={})
        newest = service.create(
            self.room_one,
            objective="Newest session must not fall back after tamper.",
            mode="quick",
        )
        with closing(sqlite3.connect(self.database_path)) as connection:
            with connection:
                connection.execute(
                    """UPDATE manual_chatgpt_events SET event_type='tampered'
                         WHERE session_id=? AND sequence_no=1""",
                    (newest["id"],),
                )
        report = DailyOperationsSummary(self.database_path).build(
            as_of_ms=self.as_of_ms,
            max_items=10,
        )
        self.assertEqual(report["manual_chatgpt"]["integrity_failed_sessions"], 1)
        incomplete_room_ids = {
            item["room_id"]
            for item in report["manual_chatgpt"]["incomplete_latest_rooms"]["items"]
        }
        self.assertNotIn(self.room_one, incomplete_room_ids)
        self.assertIn(self.room_two, incomplete_room_ids)

    def test_daily_summary_cli_returns_one_json_object_without_writes(self) -> None:
        before = hashlib.sha256(self.database_path.read_bytes()).hexdigest()
        environment = os.environ.copy()
        environment["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "backend.manual_chatgpt_operations",
                "daily-summary",
                "--database",
                str(self.database_path),
                "--as-of",
                "2026-08-26T08:00:00+08:00",
            ],
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        after = hashlib.sha256(self.database_path.read_bytes()).hexdigest()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        self.assertEqual(after, before)
        report = json.loads(completed.stdout)
        self.assertTrue(report["automation_boundary"]["report_only"])

    def test_daily_summary_markdown_is_human_readable_hash_bound_and_read_only(self) -> None:
        before = hashlib.sha256(self.database_path.read_bytes()).hexdigest()
        report = DailyOperationsSummary(self.database_path).build(
            as_of_ms=self.as_of_ms,
            waiting_expiry_hours=24,
            max_items=10,
        )
        rendered = render_daily_operations_markdown(report)
        after = hashlib.sha256(self.database_path.read_bytes()).hexdigest()
        self.assertEqual(after, before)
        self.assertIn("# AI 共创室运营摘要", rendered)
        self.assertIn("| 等待 ChatGPT | 1 |", rendered)
        self.assertIn("Waiting room", rendered)
        self.assertIn("Stale room", rendered)
        self.assertIn("USD 0.040000", rendered)
        self.assertIn("非实付，不含 ChatGPT 订阅", rendered)
        self.assertIn(report["report_sha256"], rendered)
        self.assertNotIn(str(self.database_path), rendered)

        tampered = copy.deepcopy(report)
        tampered["automation_boundary"]["provider_calls_performed"] = 1
        tampered_without_seal = dict(tampered)
        tampered_without_seal.pop("report_sha256")
        tampered["report_sha256"] = canonical_sha256(tampered_without_seal)
        with self.assertRaises(ManualChatGPTOperationsError) as boundary_error:
            render_daily_operations_markdown(tampered)
        self.assertEqual(boundary_error.exception.code, "OPERATIONS_REPORT_INVALID")

        escaped = copy.deepcopy(report)
        escaped["manual_chatgpt"]["waiting_for_chatgpt"]["items"][0]["room_title"] = (
            "Waiting [room](https://attacker.test) | # injected\nnext"
        )
        escaped_without_seal = dict(escaped)
        escaped_without_seal.pop("report_sha256")
        escaped["report_sha256"] = canonical_sha256(escaped_without_seal)
        escaped_markdown = render_daily_operations_markdown(escaped)
        self.assertIn(
            r"Waiting \[room\](https://attacker.test) \| \# injected next",
            escaped_markdown,
        )
        self.assertNotRegex(escaped_markdown, r"(?<!\\)\]\(")

    def test_daily_summary_cli_can_emit_markdown_without_writes(self) -> None:
        before = hashlib.sha256(self.database_path.read_bytes()).hexdigest()
        environment = os.environ.copy()
        environment["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "backend.manual_chatgpt_operations",
                "daily-summary",
                "--database",
                str(self.database_path),
                "--as-of",
                "2026-08-26T08:00:00+08:00",
                "--format",
                "markdown",
            ],
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        after = hashlib.sha256(self.database_path.read_bytes()).hexdigest()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        self.assertEqual(after, before)
        self.assertTrue(completed.stdout.startswith("# AI 共创室运营摘要\n"))
        self.assertIn("只读运营报告", completed.stdout)
        self.assertIn("Waiting room", completed.stdout)
        self.assertNotIn(str(self.database_path), completed.stdout)

    def test_scheduled_task_contract_is_path_free_sealed_and_non_installing(self) -> None:
        first = build_scheduled_task_contract(
            timezone_name="Asia/Shanghai",
            local_time="09:00",
            waiting_expiry_hours=24,
            max_items=50,
        )
        second = build_scheduled_task_contract(
            timezone_name="Asia/Shanghai",
            local_time="09:00",
            waiting_expiry_hours=24,
            max_items=50,
        )
        self.assertEqual(first, second)
        self.assertEqual(first["version"], SCHEDULED_TASK_CONTRACT_VERSION)
        self.assertEqual(first["schedule_suggestion"]["cadence"], "daily")
        self.assertTrue(
            first["schedule_suggestion"]["operator_confirmation_required"]
        )
        self.assertEqual(
            first["execution_surface"]["recommended"],
            "chatgpt_desktop_scheduled_task",
        )
        self.assertEqual(
            first["execution_surface"]["selected_project_mode"],
            "local_project",
        )
        self.assertFalse(first["execution_surface"]["isolated_worktree_requested"])
        self.assertTrue(
            first["execution_surface"]["isolated_worktree_requires_git_repository"]
        )
        self.assertFalse(
            first["execution_surface"]["web_task_can_directly_access_local_directory"]
        )
        self.assertFalse(first["external_state"]["external_task_created"])
        self.assertFalse(
            first["external_state"]["workspace_scheduled_tasks_enabled_verified"]
        )
        self.assertFalse(first["product_assumptions"]["account_task_limit_assumed"])
        self.assertFalse(first["product_assumptions"]["model_availability_assumed"])
        self.assertNotIn("--database", first["command_argv"])
        serialized = json.dumps(first, ensure_ascii=False)
        self.assertNotIn(str(self.database_path), serialized)
        self.assertNotIn("Provider", " ".join(first["command_argv"]))
        unsealed = dict(first)
        seal = unsealed.pop("contract_sha256")
        self.assertEqual(seal, canonical_sha256(unsealed))

    def test_scheduled_task_contract_cli_emits_one_json_object_without_database_access(self) -> None:
        environment = os.environ.copy()
        environment["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"
        environment.pop(SCHEDULED_OPERATIONS_DATABASE_ENV, None)
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "backend.manual_chatgpt_operations",
                "scheduled-task-contract",
                "--timezone",
                "Asia/Shanghai",
                "--local-time",
                "09:00",
            ],
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        contract = json.loads(completed.stdout)
        self.assertEqual(contract["version"], SCHEDULED_TASK_CONTRACT_VERSION)
        self.assertFalse(contract["external_state"]["external_task_created"])
        self.assertFalse(
            contract["required_environment"][SCHEDULED_OPERATIONS_DATABASE_ENV]
            ["value_included_in_contract"]
        )

    def test_scheduled_task_contract_rejects_ambiguous_local_time(self) -> None:
        for value in ("9:00", "09:00:00", "24:00"):
            with self.subTest(value=value):
                with self.assertRaises(ManualChatGPTOperationsError) as caught:
                    build_scheduled_task_contract(local_time=value)
                self.assertEqual(
                    caught.exception.code,
                    "SCHEDULED_LOCAL_TIME_INVALID",
                )

    def test_scheduled_daily_summary_requires_explicit_isolated_environment(self) -> None:
        base_environment = os.environ.copy()
        base_environment.pop("AI_STUDIO_SKIP_LOCAL_ENV", None)
        base_environment.pop(SCHEDULED_OPERATIONS_DATABASE_ENV, None)
        command = [
            sys.executable,
            "-m",
            "backend.manual_chatgpt_operations",
            "scheduled-daily-summary",
        ]
        missing_isolation = subprocess.run(
            command,
            cwd=Path(__file__).resolve().parents[1],
            env=base_environment,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(missing_isolation.returncode, 2)
        self.assertEqual(
            json.loads(missing_isolation.stderr)["code"],
            "SCHEDULED_ENV_ISOLATION_REQUIRED",
        )

        base_environment["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"
        missing_database = subprocess.run(
            command,
            cwd=Path(__file__).resolve().parents[1],
            env=base_environment,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(missing_database.returncode, 2)
        self.assertEqual(
            json.loads(missing_database.stderr)["code"],
            "SCHEDULED_DATABASE_REQUIRED",
        )

    def test_scheduled_daily_summary_uses_operator_bound_database_read_only(self) -> None:
        before = hashlib.sha256(self.database_path.read_bytes()).hexdigest()
        environment = os.environ.copy()
        environment["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"
        environment[SCHEDULED_OPERATIONS_DATABASE_ENV] = str(self.database_path)
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "backend.manual_chatgpt_operations",
                "scheduled-daily-summary",
                "--as-of",
                "2026-08-26T08:00:00+08:00",
            ],
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        after = hashlib.sha256(self.database_path.read_bytes()).hexdigest()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        self.assertEqual(after, before)
        self.assertTrue(completed.stdout.startswith("# AI 共创室运营摘要\n"))
        self.assertIn("Waiting room", completed.stdout)
        self.assertNotIn(str(self.database_path), completed.stdout)


class ManualChatGPTABArmExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="ai-studio-ab-arm-export-",
            ignore_cleanup_errors=True,
        )
        self.database_path = Path(self.temp_dir.name) / "studio.sqlite3"
        self.store = StudioStore(self.database_path)
        created = self.store.create_room(
            "A/B arm export room",
            "Export one verified Manual ChatGPT B arm.",
        )
        self.room_id = str(created["room"]["id"])
        self.provider = FakeReviewProvider()
        self.service = ManualChatGPTService(
            self.store,
            review_rate_card={
                "label": "ab-export-estimate",
                "input_usd_per_million_tokens": "2",
                "output_usd_per_million_tokens": "8",
            },
            providers=ProviderRegistry({
                self.provider.provider_id: self.provider,
            }),
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _frozen_session(self) -> dict[str, object]:
        session = self.service.create(
            self.room_id,
            objective="Compare the bounded B-arm workflow.",
            mode="standard",
        )
        waiting = self.service.dispatch(self.room_id, session["id"])
        imported = self.service.import_result(
            self.room_id,
            session["id"],
            json.dumps(valid_result(waiting), ensure_ascii=False),
        )
        reviewed = self.service.run_api_review(
            self.room_id,
            session["id"],
            provider_id=self.provider.provider_id,
            model="fake-review-v1",
            client_request_id="ab-arm-export-review",
            expected_result_sha256=imported["result_sha256"],
        )
        return self.service.freeze_decision(
            self.room_id,
            session["id"],
            expected_result_sha256=reviewed["result_sha256"],
            decision_card_sha256=reviewed["decision_card_sha256"],
            selected_option_id="option_1",
            acknowledgement=MANUAL_CHATGPT_FREEZE_ACKNOWLEDGEMENT,
        )

    def test_frozen_session_exports_a_schema_compatible_read_only_b_arm(self) -> None:
        frozen = self._frozen_session()
        before = hashlib.sha256(self.database_path.read_bytes()).hexdigest()
        exported = build_manual_chatgpt_ab_arm_export(
            self.database_path,
            room_id=self.room_id,
            round_id=frozen["round_id"],
        )
        after = hashlib.sha256(self.database_path.read_bytes()).hexdigest()
        self.assertEqual(after, before)
        self.assertEqual(exported["version"], AB_ARM_EXPORT_VERSION)
        self.assertEqual(exported["source"]["state"], "FROZEN")
        self.assertTrue(exported["source"]["integrity_verified"])
        self.assertFalse(
            exported["source"]["declared_historical_source_truth_verified"]
        )
        arm = exported["arm"]
        self.assertEqual(arm["model_calls"], 5)
        self.assertEqual(arm["basis"]["model_calls"], "projected")
        self.assertEqual(arm["basis"]["input_characters"], "projected")
        self.assertEqual(arm["basis"]["estimated_tokens"], "estimated")
        self.assertEqual(arm["basis"]["api_cost_usd"], "estimated")
        self.assertGreater(arm["api_cost_usd"], 0)
        self.assertEqual(arm["basis"]["wait_ms"], "measured")
        self.assertIsNone(arm["human_operation_minutes"])
        self.assertEqual(
            arm["basis"]["human_operation_minutes"],
            "unavailable",
        )
        self.assertEqual(arm["final_conclusion_id"], "option_1")
        self.assertTrue(
            exported["metric_provenance"]["model_calls"]
            ["chatgpt_panel_calls_are_user_protocol_declarations"]
        )
        self.assertTrue(
            exported["metric_provenance"]["human_operation_minutes"]
            ["must_not_be_inferred_from_wall_clock"]
        )
        self.assertFalse(exported["verification_boundary"]["complete_ab_case"])
        self.assertNotIn(
            str(self.database_path),
            json.dumps(exported, ensure_ascii=False),
        )
        unsealed = dict(exported)
        export_hash = unsealed.pop("export_sha256")
        self.assertEqual(export_hash, canonical_sha256(unsealed))

        cases = []
        for index in range(20):
            cases.append({
                "case_id": f"export_case_{index:02d}",
                "room_id": f"export_room_{index:02d}",
                "round_id": f"export_round_{index:02d}",
                "declared_source_kind": "synthetic_contract_fixture",
                "source_snapshot_sha256": "",
                "a": copy.deepcopy(arm),
                "b": copy.deepcopy(arm),
            })
        validated = validate_ab_dataset({
            "version": AB_REPLAY_DATASET_VERSION,
            "dataset_id": "exported_arm_schema_check",
            "cases": cases,
            "targets": {},
        })
        self.assertEqual(len(validated["cases"]), 20)

    def test_b_arm_export_cli_emits_one_path_free_json_object(self) -> None:
        frozen = self._frozen_session()
        environment = os.environ.copy()
        environment["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "backend.manual_chatgpt_operations",
                "historical-ab-export-b-arm",
                "--database",
                str(self.database_path),
                "--room-id",
                self.room_id,
                "--round-id",
                frozen["round_id"],
            ],
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        exported = json.loads(completed.stdout)
        self.assertEqual(exported["version"], AB_ARM_EXPORT_VERSION)
        self.assertEqual(exported["arm"]["model_calls"], 5)
        self.assertNotIn(str(self.database_path), completed.stdout)

    def test_reviewed_baseline_and_frozen_b_arm_compose_a_path_free_v3_snapshot(self) -> None:
        frozen = self._frozen_session()
        baseline_arm = replay_arm(
            model_calls=12,
            input_characters=12_000,
            estimated_tokens=3_000,
            api_cost_usd=0.12,
            wait_ms=120_000,
            human_operation_minutes=12,
            citations_passed=8,
            conclusion="option_1",
            projected=False,
        )
        baseline_path = Path(self.temp_dir.name) / "reviewed-baseline-arm.json"
        baseline_path.write_text(json.dumps(baseline_arm), encoding="utf-8")
        before = hashlib.sha256(self.database_path.read_bytes()).hexdigest()
        snapshot = build_historical_ab_source_snapshot_v3(
            self.database_path,
            case_id="reviewed_case_01",
            baseline_arm_path=baseline_path,
            baseline_room_id="legacy_room_01",
            baseline_round_id="legacy_round_01",
            room_id=self.room_id,
            round_id=frozen["round_id"],
            b_human_operation_minutes=4.5,
            acknowledgement=AB_SOURCE_REVIEW_ACKNOWLEDGEMENT,
        )
        after = hashlib.sha256(self.database_path.read_bytes()).hexdigest()
        self.assertEqual(after, before)
        self.assertEqual(snapshot["version"], AB_SOURCE_SNAPSHOT_VERSION_V3)
        self.assertEqual(snapshot["a_source"]["room_id"], "legacy_room_01")
        self.assertEqual(snapshot["b_source"]["room_id"], self.room_id)
        self.assertEqual(snapshot["b_source"]["session_id"], frozen["id"])
        self.assertTrue(snapshot["a_source"]["human_reviewed"])
        self.assertTrue(snapshot["b_source"]["human_reviewed"])
        self.assertEqual(snapshot["b"]["human_operation_minutes"], 4.5)
        self.assertEqual(snapshot["b"]["basis"]["human_operation_minutes"], "recorded")
        self.assertEqual(
            snapshot["b_source"]["human_operation_record"]["minutes"],
            4.5,
        )
        self.assertFalse(
            snapshot["b_source"]["human_operation_record"]["inferred_from_wall_clock"]
        )
        self.assertNotIn("source_snapshot_sha256", snapshot)
        serialized = json.dumps(snapshot, ensure_ascii=False)
        self.assertNotIn(str(self.database_path), serialized)
        self.assertNotIn(str(baseline_path), serialized)

        environment = os.environ.copy()
        environment["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "backend.manual_chatgpt_operations",
                "historical-ab-compose-v3",
                "--database",
                str(self.database_path),
                "--case-id",
                "reviewed_case_01",
                "--baseline-arm",
                str(baseline_path),
                "--baseline-room-id",
                "legacy_room_01",
                "--baseline-round-id",
                "legacy_round_01",
                "--room-id",
                self.room_id,
                "--round-id",
                str(frozen["round_id"]),
                "--b-human-operation-minutes",
                "4.5",
                "--acknowledgement",
                AB_SOURCE_REVIEW_ACKNOWLEDGEMENT,
            ],
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), snapshot)
        self.assertNotIn(str(self.database_path), completed.stdout)
        self.assertNotIn(str(baseline_path), completed.stdout)

    def test_v3_composer_rejects_missing_or_zero_human_operation_time(self) -> None:
        with self.assertRaises(ManualChatGPTOperationsError) as caught:
            build_historical_ab_source_snapshot_v3(
                self.database_path,
                case_id="case_01",
                baseline_arm_path=Path(self.temp_dir.name) / "unused.json",
                baseline_room_id="legacy_room_01",
                baseline_round_id="legacy_round_01",
                room_id=self.room_id,
                round_id="round_01",
                b_human_operation_minutes=0,
                acknowledgement=AB_SOURCE_REVIEW_ACKNOWLEDGEMENT,
            )
        self.assertEqual(caught.exception.code, "AB_SOURCE_HUMAN_TIME_INVALID")

    def test_v2_composer_requires_exact_review_acknowledgement(self) -> None:
        baseline_path = Path(self.temp_dir.name) / "reviewed-baseline-arm.json"
        baseline_path.write_text("{}", encoding="utf-8")
        with self.assertRaises(ManualChatGPTOperationsError) as caught:
            build_historical_ab_source_snapshot_v2(
                self.database_path,
                case_id="case_01",
                baseline_arm_path=baseline_path,
                baseline_room_id="legacy_room_01",
                baseline_round_id="legacy_round_01",
                room_id=self.room_id,
                round_id="round_01",
                acknowledgement="reviewed",
            )
        self.assertEqual(caught.exception.code, "AB_SOURCE_REVIEW_REQUIRED")

    def test_non_frozen_session_is_not_exportable(self) -> None:
        session = self.service.create(
            self.room_id,
            objective="Do not export an unfinished session.",
            mode="quick",
        )
        with self.assertRaises(ManualChatGPTOperationsError) as caught:
            build_manual_chatgpt_ab_arm_export(
                self.database_path,
                room_id=self.room_id,
                round_id=session["round_id"],
            )
        self.assertEqual(caught.exception.code, "AB_ARM_EXPORT_NOT_FROZEN")


class ManualChatGPTABReplayTests(unittest.TestCase):
    def test_24_case_contract_report_separates_primary_drivers_and_guardrails(self) -> None:
        dataset = replay_dataset()
        report = build_ab_replay_report(dataset)
        self.assertEqual(report["case_count"], 24)
        self.assertEqual(report["evidence_class"], "contract_fixture_only")
        self.assertEqual(report["metrics"]["model_calls"]["a_total"], 288)
        self.assertEqual(report["metrics"]["model_calls"]["b_total"], 120)
        self.assertEqual(report["metrics"]["wait_ms"]["comparable_cases"], 23)
        self.assertEqual(
            report["metrics"]["citation_pass_rate"]["delta_points"],
            10.0,
        )
        self.assertEqual(
            report["metrics"]["final_conclusion_change_rate"]["rate_pct"],
            25.0,
        )
        self.assertEqual(
            report["metrics"]["final_conclusion_change_rate"]["role"],
            "quality_guardrail_not_success_metric",
        )
        self.assertFalse(report["targets"]["provided"])
        self.assertTrue(report["targets"]["no_default_target_was_assumed"])
        self.assertFalse(
            report["verification_boundary"]["declared_historical_source_truth_verified"]
        )
        report_hash = report.pop("report_sha256")
        self.assertEqual(report_hash, canonical_sha256(report))

    def test_24_hash_bound_source_snapshots_build_a_calibrated_historical_report(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="ai-studio-historical-ab-",
            ignore_cleanup_errors=True,
        ) as temp_dir:
            source_directory = Path(temp_dir)
            write_historical_sources(source_directory)
            report = build_historical_ab_replay_report(
                source_directory,
                dataset_id="reviewed-historical-24",
            )
        self.assertEqual(report["case_count"], 24)
        self.assertEqual(
            report["evidence_class"],
            "hash_bound_dual_arm_historical_replay",
        )
        self.assertEqual(len(report["source_snapshot_sha256s"]), 24)
        self.assertEqual(len(report["source_bindings"]), 24)
        self.assertTrue(
            report["verification_boundary"]["dual_arm_source_bindings_structurally_verified"]
        )
        self.assertFalse(report["verification_boundary"]["source_record_contents_verified"])
        self.assertTrue(
            report["verification_boundary"]["local_source_snapshot_contents_verified"]
        )
        self.assertTrue(
            report["verification_boundary"]["source_snapshot_case_bindings_verified"]
        )
        self.assertFalse(
            report["verification_boundary"]["declared_historical_source_truth_verified"]
        )
        report_hash = report.pop("report_sha256")
        self.assertEqual(report_hash, canonical_sha256(report))

    def test_v1_sources_remain_readable_but_cannot_claim_dual_arm_binding(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="ai-studio-historical-ab-v1-",
            ignore_cleanup_errors=True,
        ) as temp_dir:
            source_directory = Path(temp_dir)
            write_historical_sources(
                source_directory,
                20,
                version=AB_SOURCE_SNAPSHOT_VERSION_V1,
            )
            status = build_historical_ab_collection_status(source_directory)
            report = build_historical_ab_replay_report(
                source_directory,
                dataset_id="legacy-v1-compatible",
            )
        self.assertTrue(status["ready_for_replay"])
        self.assertFalse(status["ready_for_dual_arm_replay"])
        self.assertFalse(status["ready_for_complete_ab_replay"])
        self.assertEqual(status["legacy_shared_identity_cases"], 20)
        self.assertTrue(
            status["verification_boundary"]["legacy_v1_shared_identity_present"]
        )
        self.assertEqual(
            report["evidence_class"],
            "hash_bound_historical_replay",
        )
        self.assertFalse(
            report["verification_boundary"]["dual_arm_source_bindings_structurally_verified"]
        )
        self.assertTrue(
            report["verification_boundary"]["legacy_v1_shared_identity_present"]
        )

    def test_v2_dual_sources_remain_readable_without_human_time_completion_claim(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="ai-studio-historical-ab-v2-",
            ignore_cleanup_errors=True,
        ) as temp_dir:
            source_directory = Path(temp_dir)
            write_historical_sources(
                source_directory,
                20,
                version=AB_SOURCE_SNAPSHOT_VERSION_V2,
            )
            status = build_historical_ab_collection_status(source_directory)
            report = build_historical_ab_replay_report(
                source_directory,
                dataset_id="dual-v2-compatible",
            )
        self.assertTrue(status["ready_for_dual_arm_replay"])
        self.assertFalse(status["ready_for_complete_ab_replay"])
        self.assertEqual(status["human_time_bound_cases"], 0)
        self.assertEqual(report["evidence_class"], "hash_bound_dual_arm_historical_replay")
        self.assertFalse(
            report["verification_boundary"]["reviewed_human_operation_records_present"]
        )

    def test_v3_complete_metric_coverage_reaches_the_strict_replay_gate(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="ai-studio-historical-ab-v3-complete-",
            ignore_cleanup_errors=True,
        ) as temp_dir:
            source_directory = Path(temp_dir)
            write_historical_sources(source_directory, 20)
            first_path = source_directory / "case-01.json"
            first = json.loads(first_path.read_text(encoding="utf-8"))
            first["a"]["wait_ms"] = 120_000
            first["a"]["basis"]["wait_ms"] = "measured"
            first["b"]["wait_ms"] = 60_000
            first["b"]["basis"]["wait_ms"] = "projected"
            first_path.write_text(json.dumps(first), encoding="utf-8")
            status = build_historical_ab_collection_status(source_directory)
            report = build_historical_ab_replay_report(
                source_directory,
                dataset_id="complete-v3-contract",
            )
        self.assertTrue(status["ready_for_complete_ab_replay"])
        self.assertTrue(status["metric_coverage"]["all_metrics_complete"])
        self.assertEqual(
            report["evidence_class"],
            "hash_bound_complete_dual_arm_historical_replay",
        )
        self.assertTrue(
            report["verification_boundary"]["complete_metric_coverage_verified"]
        )

    def test_collection_status_tracks_incomplete_and_ready_directories_read_only(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="ai-studio-historical-ab-status-",
            ignore_cleanup_errors=True,
        ) as temp_dir:
            source_directory = Path(temp_dir)
            write_historical_sources(source_directory, 19)
            before = {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in source_directory.iterdir()
            }
            collecting = build_historical_ab_collection_status(source_directory)
            after = {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in source_directory.iterdir()
            }
        self.assertEqual(after, before)
        self.assertEqual(collecting["collection_state"], "collecting")
        self.assertFalse(collecting["ready_for_replay"])
        self.assertEqual(collecting["valid_unique_cases"], 19)
        self.assertEqual(collecting["remaining_to_minimum"], 1)
        self.assertEqual(collecting["invalid_entries"], [])
        self.assertFalse(
            collecting["verification_boundary"]["declared_historical_source_truth_verified"]
        )

        with tempfile.TemporaryDirectory(
            prefix="ai-studio-historical-ab-status-",
            ignore_cleanup_errors=True,
        ) as temp_dir:
            source_directory = Path(temp_dir)
            write_historical_sources(source_directory, 20)
            ready = build_historical_ab_collection_status(source_directory)
        self.assertEqual(ready["collection_state"], "ready_for_replay")
        self.assertTrue(ready["ready_for_replay"])
        self.assertTrue(ready["ready_for_dual_arm_replay"])
        self.assertFalse(ready["ready_for_complete_ab_replay"])
        self.assertEqual(ready["valid_unique_cases"], 20)
        self.assertEqual(ready["dual_arm_bound_cases"], 20)
        self.assertEqual(ready["human_time_bound_cases"], 20)
        self.assertEqual(ready["legacy_shared_identity_cases"], 0)
        self.assertEqual(ready["remaining_to_minimum"], 0)
        self.assertEqual(len(ready["valid_sources"]), 20)
        coverage = ready["metric_coverage"]
        self.assertFalse(coverage["all_metrics_complete"])
        self.assertEqual(coverage["metrics_with_incomplete_coverage"], ["wait_ms"])
        self.assertEqual(coverage["metrics"]["wait_ms"]["comparable_cases"], 19)
        self.assertEqual(
            coverage["metrics"]["model_calls"]["basis_counts"]["a"],
            {"measured": 20},
        )
        self.assertEqual(
            coverage["metrics"]["model_calls"]["basis_counts"]["b"],
            {"projected": 20},
        )
        self.assertTrue(coverage["no_default_target_was_assumed"])
        report_hash = ready.pop("report_sha256")
        self.assertEqual(report_hash, canonical_sha256(ready))

        with tempfile.TemporaryDirectory(
            prefix="ai-studio-historical-ab-status-citation-",
            ignore_cleanup_errors=True,
        ) as temp_dir:
            source_directory = Path(temp_dir)
            write_historical_sources(source_directory, 20)
            first_path = source_directory / "case-01.json"
            first = json.loads(first_path.read_text(encoding="utf-8"))
            for arm_name in ("a", "b"):
                first[arm_name]["citation_refs_total"] = 0
                first[arm_name]["citation_refs_passed"] = 0
            first_path.write_text(json.dumps(first), encoding="utf-8")
            zero_denominator = build_historical_ab_collection_status(source_directory)
        self.assertTrue(zero_denominator["ready_for_replay"])
        self.assertEqual(
            zero_denominator["metric_coverage"]["metrics"]["citation_pass_rate"]
            ["comparable_cases"],
            19,
        )
        self.assertIn(
            "citation_pass_rate",
            zero_denominator["metric_coverage"]["metrics_with_incomplete_coverage"],
        )

    def test_collection_status_reports_invalid_duplicate_and_over_limit_entries(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="ai-studio-historical-ab-status-invalid-",
            ignore_cleanup_errors=True,
        ) as temp_dir:
            source_directory = Path(temp_dir)
            write_historical_sources(source_directory, 20)
            first = source_directory / "case-01.json"
            invalid = json.loads(first.read_text(encoding="utf-8"))
            invalid["unsupported"] = True
            first.write_text(json.dumps(invalid), encoding="utf-8")
            invalid_status = build_historical_ab_collection_status(source_directory)
        self.assertEqual(invalid_status["collection_state"], "invalid")
        self.assertFalse(invalid_status["ready_for_replay"])
        self.assertEqual(invalid_status["valid_unique_cases"], 19)
        self.assertEqual(invalid_status["invalid_entries"][0]["file"], "case-01.json")
        self.assertEqual(invalid_status["invalid_entries"][0]["code"], "AB_SOURCE_INVALID")

        with tempfile.TemporaryDirectory(
            prefix="ai-studio-historical-ab-status-duplicate-",
            ignore_cleanup_errors=True,
        ) as temp_dir:
            source_directory = Path(temp_dir)
            write_historical_sources(source_directory, 20)
            first = json.loads((source_directory / "case-01.json").read_text(encoding="utf-8"))
            second_path = source_directory / "case-02.json"
            second = json.loads(second_path.read_text(encoding="utf-8"))
            second["case_id"] = first["case_id"]
            second_path.write_text(json.dumps(second), encoding="utf-8")
            duplicate_status = build_historical_ab_collection_status(source_directory)
        self.assertEqual(duplicate_status["collection_state"], "invalid")
        self.assertEqual(duplicate_status["valid_unique_cases"], 19)
        self.assertEqual(
            duplicate_status["invalid_entries"][0]["code"],
            "AB_SOURCE_DUPLICATE",
        )

        with tempfile.TemporaryDirectory(
            prefix="ai-studio-historical-ab-status-arm-duplicate-",
            ignore_cleanup_errors=True,
        ) as temp_dir:
            source_directory = Path(temp_dir)
            write_historical_sources(source_directory, 20)
            first = json.loads((source_directory / "case-01.json").read_text(encoding="utf-8"))
            second_path = source_directory / "case-02.json"
            second = json.loads(second_path.read_text(encoding="utf-8"))
            second["a_source"] = first["a_source"]
            second_path.write_text(json.dumps(second), encoding="utf-8")
            arm_duplicate = build_historical_ab_collection_status(source_directory)
        self.assertEqual(arm_duplicate["collection_state"], "invalid")
        self.assertEqual(
            arm_duplicate["invalid_entries"][0]["code"],
            "AB_SOURCE_DUPLICATE",
        )

        with tempfile.TemporaryDirectory(
            prefix="ai-studio-historical-ab-status-human-time-",
            ignore_cleanup_errors=True,
        ) as temp_dir:
            source_directory = Path(temp_dir)
            write_historical_sources(source_directory, 20)
            first_path = source_directory / "case-01.json"
            first = json.loads(first_path.read_text(encoding="utf-8"))
            first["b_source"]["human_operation_record"]["minutes"] = 99
            first_path.write_text(json.dumps(first), encoding="utf-8")
            human_time_mismatch = build_historical_ab_collection_status(source_directory)
        self.assertEqual(human_time_mismatch["collection_state"], "invalid")
        self.assertEqual(
            human_time_mismatch["invalid_entries"][0]["code"],
            "AB_SOURCE_INVALID",
        )

        with tempfile.TemporaryDirectory(
            prefix="ai-studio-historical-ab-status-baseline-time-",
            ignore_cleanup_errors=True,
        ) as temp_dir:
            source_directory = Path(temp_dir)
            write_historical_sources(source_directory, 20)
            first_path = source_directory / "case-01.json"
            first = json.loads(first_path.read_text(encoding="utf-8"))
            first["a"]["basis"]["human_operation_minutes"] = "projected"
            first_path.write_text(json.dumps(first), encoding="utf-8")
            baseline_time_invalid = build_historical_ab_collection_status(source_directory)
        self.assertEqual(baseline_time_invalid["collection_state"], "invalid")
        self.assertEqual(
            baseline_time_invalid["invalid_entries"][0]["code"],
            "AB_SOURCE_INVALID",
        )

        with tempfile.TemporaryDirectory(
            prefix="ai-studio-historical-ab-status-over-",
            ignore_cleanup_errors=True,
        ) as temp_dir:
            source_directory = Path(temp_dir)
            write_historical_sources(source_directory, 32)
            over_limit = build_historical_ab_collection_status(source_directory)
        self.assertEqual(over_limit["collection_state"], "over_limit")
        self.assertFalse(over_limit["ready_for_replay"])
        self.assertEqual(over_limit["valid_unique_cases"], 31)
        self.assertEqual(over_limit["discovered_entries"], 32)
        self.assertEqual(over_limit["scanned_entries"], 31)
        self.assertTrue(over_limit["scan_truncated"])
        self.assertEqual(over_limit["capacity_remaining"], 0)


class ManualChatGPTOperationsCliTests(unittest.TestCase):
    def test_ab_replay_cli_emits_one_json_report_without_loading_local_env(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="ai-studio-operations-cli-",
            ignore_cleanup_errors=True,
        ) as temp_dir:
            dataset_path = Path(temp_dir) / "dataset.json"
            dataset_path.write_text(
                json.dumps(replay_dataset(), ensure_ascii=False),
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "backend.manual_chatgpt_operations",
                    "ab-replay",
                    "--dataset",
                    str(dataset_path),
                ],
                cwd=Path(__file__).resolve().parents[1],
                env=environment,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        report = json.loads(completed.stdout)
        self.assertEqual(report["case_count"], 24)
        self.assertEqual(report["verification_boundary"]["provider_calls_performed"], 0)
        report_hash = report.pop("report_sha256")
        self.assertEqual(report_hash, canonical_sha256(report))

    def test_historical_replay_cli_reads_only_hash_bound_source_snapshots(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="ai-studio-historical-ab-cli-",
            ignore_cleanup_errors=True,
        ) as temp_dir:
            source_directory = Path(temp_dir)
            write_historical_sources(source_directory)
            environment = os.environ.copy()
            environment["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "backend.manual_chatgpt_operations",
                    "historical-ab-replay",
                    "--source-directory",
                    str(source_directory),
                    "--dataset-id",
                    "reviewed-historical-24",
                ],
                cwd=Path(__file__).resolve().parents[1],
                env=environment,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        report = json.loads(completed.stdout)
        self.assertEqual(
            report["evidence_class"],
            "hash_bound_dual_arm_historical_replay",
        )
        self.assertEqual(report["verification_boundary"]["provider_calls_performed"], 0)

    def test_historical_collection_status_cli_accepts_an_incomplete_read_only_set(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="ai-studio-historical-ab-status-cli-",
            ignore_cleanup_errors=True,
        ) as temp_dir:
            source_directory = Path(temp_dir)
            write_historical_sources(source_directory, 19)
            before = {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in source_directory.iterdir()
            }
            environment = os.environ.copy()
            environment["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "backend.manual_chatgpt_operations",
                    "historical-ab-status",
                    "--source-directory",
                    str(source_directory),
                ],
                cwd=Path(__file__).resolve().parents[1],
                env=environment,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            after = {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in source_directory.iterdir()
            }
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        self.assertEqual(after, before)
        status = json.loads(completed.stdout)
        self.assertEqual(status["collection_state"], "collecting")
        self.assertEqual(status["remaining_to_minimum"], 1)
        self.assertFalse(status["ready_for_replay"])
        self.assertFalse(status["verification_boundary"]["source_directory_returned"])
        self.assertNotIn(str(source_directory), completed.stdout)

    def test_historical_replay_rejects_unreviewed_counts_and_extra_fields(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="ai-studio-historical-ab-invalid-",
            ignore_cleanup_errors=True,
        ) as temp_dir:
            source_directory = Path(temp_dir)
            write_historical_sources(source_directory, 19)
            with self.assertRaises(ManualChatGPTOperationsError) as count_error:
                build_historical_ab_replay_report(
                    source_directory,
                    dataset_id="too-few",
                )
            self.assertEqual(count_error.exception.code, "AB_CASE_COUNT_INVALID")

        with tempfile.TemporaryDirectory(
            prefix="ai-studio-historical-ab-invalid-",
            ignore_cleanup_errors=True,
        ) as temp_dir:
            source_directory = Path(temp_dir)
            write_historical_sources(source_directory, 20)
            first = source_directory / "case-01.json"
            invalid = json.loads(first.read_text(encoding="utf-8"))
            invalid["unsupported"] = True
            first.write_text(json.dumps(invalid), encoding="utf-8")
            with self.assertRaises(ManualChatGPTOperationsError) as schema_error:
                build_historical_ab_replay_report(
                    source_directory,
                    dataset_id="invalid-source",
                )
            self.assertEqual(schema_error.exception.code, "AB_SOURCE_INVALID")


class ManualChatGPTABReplayValidationTests(unittest.TestCase):
    def test_targets_are_input_only_and_evaluated_without_becoming_defaults(self) -> None:
        dataset = replay_dataset()
        dataset["targets"] = {
            "model_calls_reduction_pct_min": 50,
            "final_conclusion_change_rate_pct_max": 30,
        }
        report = build_ab_replay_report(dataset)
        self.assertEqual(
            report["targets"]["evaluation"]["model_calls_reduction_pct_min"]["status"],
            "met",
        )
        self.assertEqual(
            report["targets"]["evaluation"]["final_conclusion_change_rate_pct_max"]["status"],
            "met",
        )

    def test_case_count_duplicates_ranges_and_availability_fail_closed(self) -> None:
        too_few = replay_dataset(19)
        with self.assertRaises(ManualChatGPTOperationsError) as count_error:
            validate_ab_dataset(too_few)
        self.assertEqual(count_error.exception.code, "AB_CASE_COUNT_INVALID")

        duplicate = replay_dataset()
        duplicate["cases"][1]["case_id"] = duplicate["cases"][0]["case_id"]
        with self.assertRaises(ManualChatGPTOperationsError):
            validate_ab_dataset(duplicate)

        invalid_basis = replay_dataset()
        invalid_basis["cases"][0]["a"]["wait_ms"] = None
        invalid_basis["cases"][0]["a"]["basis"]["wait_ms"] = "measured"
        with self.assertRaises(ManualChatGPTOperationsError):
            validate_ab_dataset(invalid_basis)

        invalid_target = replay_dataset()
        invalid_target["targets"] = {"model_calls_reduction_pct_min": 101}
        with self.assertRaises(ManualChatGPTOperationsError):
            validate_ab_dataset(invalid_target)

    def test_historical_label_remains_declared_not_truth_verified(self) -> None:
        dataset = replay_dataset(20)
        for item in dataset["cases"]:
            item["declared_source_kind"] = "historical_round"
            item["source_snapshot_sha256"] = "c" * 64
        report = build_ab_replay_report(dataset)
        self.assertEqual(report["evidence_class"], "declared_historical_replay")
        self.assertFalse(
            report["verification_boundary"]["declared_historical_source_truth_verified"]
        )


if __name__ == "__main__":
    unittest.main()
