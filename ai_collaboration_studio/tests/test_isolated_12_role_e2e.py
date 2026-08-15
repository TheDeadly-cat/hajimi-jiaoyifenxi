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
from pathlib import Path

from scripts.run_isolated_12_role_e2e import (
    CallLedger,
    DryRunMarketService,
    MarketGateFailed,
    ProviderCallBudgetExceeded,
    validate_market_snapshot,
)


PROJECT_DIR = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_DIR / "scripts" / "run_isolated_12_role_e2e.py"


SOCKET_DENIED_RUNNER = r"""
import runpy
import socket
import sys
from unittest.mock import patch

script_path = sys.argv[1]
script_args = sys.argv[2:]
blocked_calls = []
original_create_connection = socket.create_connection
original_socket_connect = socket.socket.connect

def block_socket(*args, **kwargs):
    blocked_calls.append("socket")
    raise AssertionError("isolated dry-run attempted a socket connection")

sys.argv = [script_path, *script_args]
exit_code = 0
with patch.object(socket, "create_connection", block_socket), patch.object(
    socket.socket,
    "connect",
    block_socket,
):
    try:
        runpy.run_path(script_path, run_name="__main__")
    except SystemExit as exc:
        exit_code = exc.code if isinstance(exc.code, int) else 1

if (
    socket.create_connection is not original_create_connection
    or socket.socket.connect is not original_socket_connect
):
    raise AssertionError("socket patches were not restored")
if blocked_calls:
    raise AssertionError("isolated dry-run attempted a socket connection")
raise SystemExit(exit_code)
"""


MEMBERS = [
    ("投资委员会主持人", "facilitator", "facilitate", "deepseek", "deepseek-v4-pro"),
    ("存储周期分析师", "sector", "analysis", "deepseek", "deepseek-v4-pro"),
    ("硬盘产业分析师", "sector", "analysis", "deepseek", "deepseek-v4-pro"),
    ("基本面分析师", "fundamental", "analysis", "deepseek", "deepseek-v4-pro"),
    ("技术与资金分析师", "technical", "analysis", "doubao", "doubao-seed-2-0-lite-260215"),
    ("多头研究员", "bull", "debate", "deepseek", "deepseek-v4-pro"),
    ("空头研究员", "bear", "debate", "deepseek", "deepseek-v4-pro"),
    ("风险经理", "risk", "risk", "deepseek", "deepseek-v4-pro"),
    ("数据质量官", "data_guardian", "analysis", "deepseek", "deepseek-v4-pro"),
    ("模拟交易员", "paper_trader", "plan", "doubao", "doubao-seed-2-0-lite-260215"),
    ("投委会决策经理", "portfolio_manager", "decision", "deepseek", "deepseek-v4-pro"),
    ("新闻与情绪分析师", "sentiment", "analysis", "doubao", "doubao-seed-2-0-lite-260215"),
]


WORKFLOW_POLICY = {
    "version": 1,
    "stage_order": ["facilitate", "analysis", "debate", "plan", "risk", "decision"],
    "minimum_stage_coverage": {
        "facilitate": 1,
        "analysis": 5,
        "debate": 2,
        "plan": 1,
        "risk": 1,
        "decision": 1,
    },
    "required_coverage": [
        {
            "id": "facilitation",
            "label": "主持与目标守门",
            "minimum": 1,
            "any_of": {"stances": ["facilitator"], "capabilities": []},
            "is_counterargument": False,
        },
        {
            "id": "counterargument",
            "label": "空头反证",
            "minimum": 1,
            "any_of": {"stances": ["bear"], "capabilities": []},
            "is_counterargument": True,
        },
    ],
    "minimum_successful_members": 11,
    "max_turns_per_member": 2,
    "follow_up_budget": 6,
    "user_confirmation_required": True,
    "execution_capability": "none",
    "live_trading_allowed": False,
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def create_source_fixture(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(
            """
            CREATE TABLE rooms (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                objective TEXT NOT NULL,
                domain TEXT NOT NULL,
                category TEXT NOT NULL,
                template_id TEXT NOT NULL,
                discussion_mode TEXT NOT NULL,
                moderator_member_id TEXT NOT NULL,
                workflow_policy_json TEXT NOT NULL
            );
            CREATE TABLE members (
                id TEXT PRIMARY KEY,
                room_id TEXT NOT NULL,
                name TEXT NOT NULL,
                identity TEXT NOT NULL,
                instructions TEXT NOT NULL,
                responsibilities TEXT NOT NULL,
                boundaries TEXT NOT NULL,
                stance TEXT NOT NULL,
                workflow_stage TEXT NOT NULL,
                capabilities_json TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                enabled INTEGER NOT NULL,
                position INTEGER NOT NULL
            );
            """
        )
        connection.execute(
            """INSERT INTO rooms(
                   id,title,objective,domain,category,template_id,discussion_mode,
                   moderator_member_id,workflow_policy_json
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                "room_storage",
                "本地十二角色夹具",
                "比较四只存储股并保留用户最终决定权。",
                "market_research",
                "交易研究 / 美股",
                "us_storage_committee",
                "dynamic",
                "member_01",
                json.dumps(WORKFLOW_POLICY, ensure_ascii=False),
            ),
        )
        for position, (name, stance, stage, provider, model) in enumerate(MEMBERS, start=1):
            connection.execute(
                """INSERT INTO members(
                       id,room_id,name,identity,instructions,responsibilities,boundaries,
                       stance,workflow_stage,capabilities_json,provider,model,enabled,position
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    f"member_{position:02d}",
                    "room_storage",
                    name,
                    f"{name}的测试身份",
                    "只基于冻结证据推进讨论。",
                    "完成本职责并回应前序观点。",
                    "只做研究、回测与模拟观察，禁止真实下单。",
                    stance,
                    stage,
                    "[]",
                    provider,
                    model,
                    1,
                    position,
                ),
            )


class IsolatedTwelveRoleE2ETests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.source_db = Path(self.temp_dir.name) / "source.sqlite3"
        create_source_fixture(self.source_db)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def run_cli(
        self,
        *args: str,
        clear_local_env: bool = False,
    ) -> tuple[subprocess.CompletedProcess[str], dict]:
        child_env = os.environ.copy()
        child_env["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"
        if clear_local_env:
            child_env.pop("AI_STUDIO_SKIP_LOCAL_ENV", None)
        for key in (
            "OPENAI_API_KEY",
            "DEEPSEEK_API_KEY",
            "DOUBAO_API_KEY",
            "ARK_API_KEY",
            "GLM_API_KEY",
            "ZHIPU_API_KEY",
        ):
            child_env.pop(key, None)
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                SOCKET_DENIED_RUNNER,
                str(SCRIPT_PATH),
                *args,
                "--source-db",
                str(self.source_db),
            ],
            cwd=PROJECT_DIR,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
            env=child_env,
        )
        self.assertEqual(completed.stderr, "")
        self.assertEqual(len(completed.stdout.strip().splitlines()), 1)
        return completed, json.loads(completed.stdout)

    def test_direct_dry_run_defaults_to_the_disposable_environment(self) -> None:
        completed, report = self.run_cli(
            "--dry-run",
            clear_local_env=True,
        )

        self.assertEqual(completed.returncode, 0)
        self.assertTrue(report["ok"])
        self.assertTrue(report["source_database"]["unchanged"])
        self.assertEqual(report["providers"]["external_network_calls"], 0)

    def test_real_mode_requires_explicit_local_env_opt_in_after_ack(self) -> None:
        completed, report = self.run_cli(
            "--execute-real",
            "--acknowledge-paid-calls",
            "MAX_28_PROVIDER_CALLS",
        )

        self.assertEqual(completed.returncode, 2)
        self.assertFalse(report["ok"])
        self.assertEqual(report["error"]["code"], "REAL_ENV_OPT_IN_REQUIRED")

    def test_dry_run_covers_all_gates_without_external_calls_or_source_writes(self) -> None:
        before = sha256(self.source_db)

        completed, report = self.run_cli("--dry-run")

        self.assertEqual(completed.returncode, 0)
        self.assertTrue(report["ok"])
        self.assertEqual(sha256(self.source_db), before)
        self.assertTrue(report["source_database"]["query_only_asserted"])
        self.assertEqual(report["source_database"]["read_connection_total_changes"], 0)
        self.assertTrue(report["source_database"]["unchanged"])
        self.assertEqual(report["source_room"]["provider_counts"], {
            "deepseek": 9,
            "doubao": 3,
        })
        self.assertTrue(report["source_room"]["moderator_explicit"])
        self.assertEqual(report["source_room"]["moderator_member_id"], "member_01")
        self.assertEqual(report["market"]["snapshot_calls"], 1)
        self.assertEqual(report["market"]["ready_symbol_count"], 4)
        # Rules-first resolves six unambiguous scheduling points locally:
        # 2 preflights + 12 speakers + 6 true director ambiguities + 1 artifact.
        self.assertEqual(report["providers"]["total_calls"], 21)
        self.assertLessEqual(report["providers"]["total_calls"], 28)
        self.assertEqual(report["providers"]["external_network_calls"], 0)
        self.assertEqual(report["providers"]["openai_network_calls"], 0)
        self.assertEqual(report["providers"]["retry_count"], 0)
        self.assertEqual(report["providers"]["cross_provider_fallback_count"], 0)
        self.assertEqual(report["round"]["status"], "COMPLETED")
        self.assertEqual(report["round"]["unique_successful_members"], 12)
        self.assertEqual(report["round"]["turn_contract_version"], "turn_contract_v1")
        self.assertEqual(report["round"]["qualified_turn_contract_count"], 12)
        self.assertEqual(report["round"]["unqualified_turn_contract_count"], 0)
        self.assertEqual(report["round"]["hidden_block_leak_count"], 0)
        self.assertEqual(
            report["round"]["validated_response_edge_count"],
            report["round"]["required_response_edge_count"],
        )
        self.assertGreater(report["round"]["validated_response_edge_count"], 0)
        self.assertEqual(report["round"]["first_turn_provider_counts"], {
            "deepseek": 9,
            "doubao": 3,
        })
        self.assertGreater(
            report["round"]["ai_speak_decisions"]
            + report["round"]["rules_first_speak_decisions"],
            0,
        )
        self.assertEqual(report["checkpoint"]["successful_member_count"], 12)
        self.assertEqual(report["checkpoint"]["failed_member_count"], 0)
        self.assertTrue(report["moderator"]["source_explicit"])
        self.assertEqual(report["moderator"]["source_member_id"], "member_01")
        self.assertTrue(report["moderator"]["mapped_to_cloned_member"])
        self.assertEqual(report["moderator"]["provider"], "deepseek")
        self.assertTrue(report["moderator"]["checkpoint_matches"])
        self.assertGreater(report["moderator"]["director_attempt_count"], 0)
        self.assertTrue(report["moderator"]["director_attempts_match"])
        self.assertEqual(report["convergence"]["before_artifact"], "DRAFT_REQUIRED")
        self.assertEqual(
            report["convergence"]["after_artifact"],
            "EVIDENCE_REVIEW_REQUIRED",
        )
        self.assertEqual(
            report["convergence"]["after_fixture_user"],
            "USER_SUPPORTED",
        )
        self.assertTrue(report["convergence"]["research_ready"])
        self.assertTrue(report["convergence"]["decision_slate_ready"])
        self.assertTrue(report["convergence"]["can_present_candidate_best"])
        self.assertFalse(report["convergence"]["can_autonomously_decide"])
        self.assertFalse(report["convergence"]["user_confirmation_required"])
        self.assertEqual(report["artifact"]["initial_status"], "DRAFT")
        self.assertEqual(report["artifact"]["initial_version"], 1)
        self.assertEqual(report["artifact"]["status"], "CONFIRMED")
        self.assertEqual(report["artifact"]["version"], 4)
        self.assertEqual(
            report["artifact"]["confirmed_by"],
            "isolated_fixture_user",
        )
        self.assertTrue(report["artifact"]["round_bound"])
        self.assertTrue(report["artifact"]["model_generated"])
        self.assertEqual(report["artifact"]["generation_mode"], "fixture_provider")
        self.assertFalse(report["artifact"]["external_model_generated"])
        self.assertGreaterEqual(report["artifact"]["decision_option_count"], 2)
        self.assertTrue(report["artifact"]["preferred_option_recorded"])
        self.assertTrue(report["artifact"]["decision_rationale_recorded"])
        self.assertEqual(
            report["artifact"]["initial_evidence_count"],
            report["artifact"]["initial_unreviewed_evidence_count"],
        )
        self.assertEqual(report["artifact"]["unreviewed_evidence_count"], 0)
        self.assertGreater(report["artifact"]["reviewed_evidence_count"], 0)
        self.assertEqual(
            report["artifact"]["evidence_count"],
            report["artifact"]["reviewed_evidence_count"],
        )
        self.assertEqual(report["artifact"]["market_snapshot_evidence_count"], 1)
        self.assertTrue(report["artifact"]["market_snapshot_evidence_exact"])
        self.assertTrue(report["fixture_user_gate"]["applied"])
        self.assertEqual(
            report["fixture_user_gate"]["actor"],
            "isolated_fixture_user",
        )
        self.assertTrue(report["fixture_user_gate"]["simulated_user_action"])
        self.assertFalse(report["fixture_user_gate"]["represents_real_user"])
        self.assertTrue(
            report["fixture_user_gate"][
                "human_confirmation_still_required_for_real_run"
            ]
        )
        self.assertTrue(report["fixture_user_gate"]["counter_message_id"])
        self.assertEqual(report["fixture_user_gate"]["provider_calls_delta"], 0)
        self.assertEqual(
            report["fixture_user_gate"]["external_provider_calls_delta"],
            0,
        )
        # The exact single-name contract reads only its bound MU history.
        self.assertEqual(report["fixture_user_gate"]["market_fixture_calls_delta"], 1)
        self.assertEqual(
            report["fixture_user_gate"]["negative_checks"],
            {
                "unreviewed_artifact_rejected": True,
                "stale_artifact_version_rejected": True,
                "stale_decision_version_rejected": True,
            },
        )
        self.assertTrue(report["user_decision"]["present"])
        self.assertEqual(report["user_decision"]["action"], "support")
        self.assertEqual(
            report["user_decision"]["decision_version"],
            "artifact_user_decision_v2",
        )
        self.assertTrue(report["user_decision"]["ai_preferred_option_id"])
        self.assertEqual(
            report["user_decision"]["selected_option_id"],
            report["user_decision"]["ai_preferred_option_id"],
        )
        self.assertTrue(report["user_decision"]["selected_is_ai_preferred"])
        self.assertTrue(
            report["user_decision"]["candidate_binding_integrity_ok"]
        )
        self.assertTrue(report["user_decision"]["decision_record_integrity_ok"])
        self.assertEqual(
            report["user_decision"]["created_by"],
            "isolated_fixture_user",
        )
        self.assertTrue(report["user_decision"]["exact_artifact_version"])
        self.assertTrue(report["paper_portfolio"]["present"])
        self.assertEqual(report["paper_portfolio"]["status"], "CONFIRMED")
        self.assertTrue(
            report["paper_portfolio"]["candidate_simulation_binding_ready"]
        )
        self.assertEqual(
            report["paper_portfolio"][
                "candidate_simulation_contract_version"
            ],
            "candidate_simulation_contract_v1",
        )
        self.assertEqual(
            report["paper_portfolio"]["confirmed_by"],
            "isolated_fixture_user",
        )
        self.assertTrue(report["paper_portfolio"]["risk_gate_ready"])
        self.assertEqual(report["paper_portfolio"]["execution_capability"], "none")
        self.assertFalse(report["paper_portfolio"]["live_trading_allowed"])
        self.assertTrue(report["storage_sample_acceptance"]["evaluated"])
        self.assertEqual(report["storage_sample_acceptance"]["state"], "accepted")
        self.assertTrue(report["storage_sample_acceptance"]["acceptance_ready"])
        self.assertTrue(report["storage_sample_acceptance"]["meeting_reviewed"])
        self.assertTrue(
            report["storage_sample_acceptance"]["research_sample_ready"]
        )
        self.assertEqual(
            report["storage_sample_acceptance"]["user_decision_action"],
            "support",
        )
        self.assertTrue(
            report["storage_sample_acceptance"]["paper_portfolio_gate_ready"]
        )
        self.assertFalse(
            report["storage_sample_acceptance"]["statistical_validation_ready"]
        )
        self.assertEqual(report["storage_sample_acceptance"]["provider_calls"], 0)
        self.assertEqual(report["storage_sample_acceptance"]["market_calls"], 0)
        self.assertTrue(report["storage_sample_acceptance"]["read_only"])
        self.assertTrue(report["user_confirmation"]["artifact_confirmed"])
        self.assertEqual(report["user_confirmation"]["confirmed_observations"], 0)
        self.assertEqual(
            report["user_confirmation"]["confirmed_paper_portfolios"],
            1,
        )
        self.assertTrue(report["isolation"]["temporary_database_removed"])

    def test_openai_assignment_is_rejected_before_any_provider_call(self) -> None:
        with closing(sqlite3.connect(self.source_db)) as connection, connection:
            connection.execute(
                "UPDATE members SET provider='openai',model='forbidden' WHERE position=1"
            )
        before = sha256(self.source_db)

        completed, report = self.run_cli("--dry-run")

        self.assertEqual(completed.returncode, 1)
        self.assertFalse(report["ok"])
        self.assertEqual(report["error"]["code"], "OPENAI_FORBIDDEN")
        self.assertEqual(report["source_room"]["openai_assignments"], 1)
        self.assertEqual(report["providers"]["total_calls"], 0)
        self.assertEqual(report["providers"]["external_network_calls"], 0)
        self.assertEqual(report["providers"]["openai_network_calls"], 0)
        self.assertTrue(report["source_database"]["unchanged"])
        self.assertEqual(sha256(self.source_db), before)

    def test_explicit_non_default_moderator_is_mapped_into_isolated_round(self) -> None:
        with closing(sqlite3.connect(self.source_db)) as connection, connection:
            connection.execute(
                "UPDATE rooms SET moderator_member_id='member_05' WHERE id='room_storage'"
            )
        before = sha256(self.source_db)

        completed, report = self.run_cli("--dry-run")

        self.assertEqual(completed.returncode, 0)
        self.assertTrue(report["ok"])
        self.assertEqual(report["source_room"]["moderator_member_id"], "member_05")
        self.assertEqual(report["moderator"]["source_member_id"], "member_05")
        self.assertEqual(report["moderator"]["provider"], "doubao")
        self.assertTrue(report["moderator"]["mapped_to_cloned_member"])
        self.assertTrue(report["moderator"]["checkpoint_matches"])
        self.assertTrue(report["moderator"]["director_attempts_match"])
        self.assertTrue(report["source_database"]["unchanged"])
        self.assertEqual(sha256(self.source_db), before)

    def test_missing_explicit_moderator_is_rejected_before_any_provider_call(self) -> None:
        with closing(sqlite3.connect(self.source_db)) as connection, connection:
            connection.execute(
                "UPDATE rooms SET moderator_member_id='' WHERE id='room_storage'"
            )
        before = sha256(self.source_db)

        completed, report = self.run_cli("--dry-run")

        self.assertEqual(completed.returncode, 1)
        self.assertFalse(report["ok"])
        self.assertEqual(report["error"]["code"], "SOURCE_ROOM_INVALID")
        self.assertFalse(report["source_room"]["moderator_explicit"])
        self.assertEqual(report["providers"]["total_calls"], 0)
        self.assertEqual(report["providers"]["external_network_calls"], 0)
        self.assertTrue(report["source_database"]["unchanged"])
        self.assertEqual(sha256(self.source_db), before)

    def test_report_file_is_exclusive_and_matches_stdout(self) -> None:
        report_file = Path(self.temp_dir.name) / "safe-report.json"

        completed, report = self.run_cli(
            "--dry-run",
            "--report-file",
            str(report_file),
        )

        self.assertEqual(completed.returncode, 0)
        self.assertTrue(report_file.is_file())
        self.assertEqual(json.loads(report_file.read_text(encoding="utf-8")), report)

        completed_again, blocked = self.run_cli(
            "--dry-run",
            "--report-file",
            str(report_file),
        )
        self.assertEqual(completed_again.returncode, 2)
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["error"]["code"], "REPORT_FILE_INVALID")
        self.assertEqual(json.loads(report_file.read_text(encoding="utf-8")), report)

    def test_call_ledger_blocks_the_twenty_ninth_call_before_reservation(self) -> None:
        ledger = CallLedger(mode="dry-run")
        for _ in range(28):
            handle = ledger.begin(
                provider="deepseek",
                model="dry",
                kind="speaker",
                external=False,
            )
            ledger.finish(handle, ok=True)

        with self.assertRaises(ProviderCallBudgetExceeded):
            ledger.begin(
                provider="deepseek",
                model="dry",
                kind="speaker",
                external=False,
            )

        self.assertEqual(ledger.summary()["total_calls"], 28)
        self.assertEqual(ledger.summary()["external_network_calls"], 0)

    def test_strict_market_evidence_preflight_accepts_clean_ready_evidence(self) -> None:
        snapshot = DryRunMarketService().capture()

        validate_market_snapshot(snapshot)

    def test_strict_market_evidence_preflight_rejects_degraded_evidence(self) -> None:
        snapshot = DryRunMarketService().capture()
        snapshot["evidence"]["state"] = "degraded"

        with self.assertRaisesRegex(MarketGateFailed, "研究证据"):
            validate_market_snapshot(snapshot)

    def test_strict_market_evidence_preflight_rejects_nested_source_errors(self) -> None:
        snapshot = DryRunMarketService().capture()
        snapshot["evidence"]["official_earnings_materials"] = {
            "state": "partial",
            "rows": [],
            "source_errors": [{
                "source": "official_company_ir_materials",
                "code": "EARNINGS_MATERIAL_HUB_ERROR",
                "message": "fixture",
            }],
        }

        with self.assertRaisesRegex(MarketGateFailed, "嵌套来源错误"):
            validate_market_snapshot(copy.deepcopy(snapshot))

    def test_strict_market_preflight_allows_missing_normal_and_enum_normal_status(self) -> None:
        snapshot = DryRunMarketService().capture()
        snapshot["rows"][1]["security_status"] = "NORMAL"
        snapshot["rows"][2]["security_status"] = "SecurityStatus.NORMAL"
        snapshot["rows"][3]["security_status"] = "Futu.SecurityStatus.NORMAL"

        validate_market_snapshot(snapshot)

    def test_strict_market_preflight_rejects_suspended_row(self) -> None:
        snapshot = DryRunMarketService().capture()
        snapshot["rows"][0]["suspended"] = True

        with self.assertRaisesRegex(MarketGateFailed, "4/4 ready"):
            validate_market_snapshot(snapshot)

    def test_strict_market_preflight_rejects_explicit_abnormal_security_status(self) -> None:
        for status in ("SUSPENDED", "SecurityStatus.DELISTED"):
            with self.subTest(status=status):
                snapshot = DryRunMarketService().capture()
                snapshot["rows"][0]["security_status"] = status

                with self.assertRaisesRegex(MarketGateFailed, "4/4 ready"):
                    validate_market_snapshot(snapshot)


if __name__ == "__main__":
    unittest.main()
