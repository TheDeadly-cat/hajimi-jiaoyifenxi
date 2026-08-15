from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from backend.providers.base import ProviderResponse
from backend.store import StudioStore
from scripts.run_isolated_12_role_e2e import BudgetedProvider, CallLedger


PROJECT_DIR = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_DIR / "scripts" / "run_isolated_generic_room_e2e.py"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class IsolatedGenericRoomE2ETests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.source_db = Path(self.temp_dir.name) / "source.sqlite3"
        StudioStore(self.source_db)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def run_cli(
        self,
        *args: str,
        clear_local_env: bool = False,
    ) -> tuple[subprocess.CompletedProcess[str], dict]:
        child_env = None
        if clear_local_env:
            child_env = os.environ.copy()
            child_env.pop("AI_STUDIO_SKIP_LOCAL_ENV", None)
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--source-db",
                str(self.source_db),
                *args,
            ],
            cwd=PROJECT_DIR,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            env=child_env,
        )
        self.assertEqual(len(completed.stdout.strip().splitlines()), 1)
        return completed, json.loads(completed.stdout)

    def test_direct_dry_run_defaults_to_the_disposable_environment(self) -> None:
        completed, report = self.run_cli(
            "--dry-run",
            clear_local_env=True,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(report["ok"])
        self.assertTrue(report["source_database"]["unchanged"])
        self.assertEqual(report["providers"]["external_network_calls"], 0)

    def test_dry_run_proves_generic_dynamic_multi_provider_decision_slate(self) -> None:
        before = sha256(self.source_db)

        completed, report = self.run_cli("--dry-run")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(report["ok"])
        self.assertEqual(report["scenario"], "generic_dynamic_decision_slate")
        self.assertEqual(sha256(self.source_db), before)
        self.assertTrue(report["source_database"]["query_only_asserted"])
        self.assertTrue(report["source_database"]["unchanged"])
        self.assertEqual(report["routing"]["isolated_acceptance_provider_counts"], {
            "deepseek": 3,
            "doubao": 1,
        })
        self.assertEqual(report["routing"]["source_capability_pack_ids"], [])
        self.assertEqual(
            report["routing"]["isolated_capability_pack_ids"],
            [],
        )
        self.assertFalse(report["routing"]["isolated_turn_contract_capability"])
        self.assertTrue(report["routing"]["core_turn_contract_protocol"])
        self.assertTrue(report["provider_preflight"]["ready"])
        self.assertEqual(report["providers"]["openai_network_calls"], 0)
        self.assertEqual(report["providers"]["external_network_calls"], 0)
        self.assertLessEqual(report["providers"]["total_calls"], 16)
        self.assertEqual(report["round"]["status"], "COMPLETED")
        self.assertEqual(report["round"]["unique_successful_members"], 4)
        self.assertEqual(report["round"]["turn_contract_version"], "turn_contract_v1")
        self.assertEqual(
            report["round"]["qualified_turn_contract_count"],
            report["round"]["completed_turns"],
        )
        self.assertEqual(report["round"]["qualified_unique_member_count"], 4)
        self.assertEqual(report["round"]["unqualified_turn_contract_count"], 0)
        self.assertEqual(report["round"]["hidden_block_leak_count"], 0)
        self.assertEqual(
            report["round"]["validated_response_edge_count"],
            report["round"]["required_response_edge_count"],
        )
        self.assertGreater(report["round"]["validated_response_edge_count"], 0)
        self.assertGreater(
            report["round"]["ai_speak_decisions"]
            + report["round"]["rules_first_speak_decisions"],
            0,
        )
        self.assertEqual(report["artifact"]["draft_status"], "DRAFT")
        self.assertEqual(report["artifact"]["reviewed_status"], "DRAFT")
        self.assertEqual(report["artifact"]["status"], "CONFIRMED")
        self.assertEqual(report["artifact"]["generation_mode"], "fixture_provider")
        self.assertFalse(report["artifact"]["external_model_generated"])
        self.assertTrue(report["artifact"]["turn_contract_projection_recorded"])
        self.assertTrue(report["artifact"]["deterministic_decision_projection"])
        self.assertEqual(
            report["artifact"]["projected_qualified_message_count"],
            report["round"]["completed_turns"],
        )
        self.assertGreaterEqual(report["artifact"]["projected_decision_option_count"], 2)
        self.assertGreaterEqual(report["artifact"]["decision_option_count"], 2)
        self.assertTrue(report["artifact"]["preferred_option_recorded"])
        self.assertTrue(report["artifact"]["decision_rationale_recorded"])
        self.assertTrue(report["artifact"]["evidence_review_ready"])
        self.assertGreater(report["artifact"]["reviewed_evidence_count"], 0)
        self.assertTrue(report["artifact"]["confirmed"])
        self.assertTrue(report["artifact"]["confirmed_by_fixture_user"])
        self.assertEqual(report["convergence"]["after_artifact"], "EVIDENCE_REVIEW_REQUIRED")
        self.assertEqual(report["convergence"]["after_confirmation"], "READY_FOR_USER_DECISION")
        self.assertTrue(report["convergence"]["after_confirmation_can_present_candidate_best"])
        self.assertTrue(report["convergence"]["turn_contract_ready"])
        self.assertFalse(report["convergence"]["can_autonomously_decide"])
        self.assertTrue(report["isolation"]["temporary_database_removed"])

    def test_real_mode_requires_explicit_paid_call_acknowledgement(self) -> None:
        completed, report = self.run_cli("--execute-real")

        self.assertEqual(completed.returncode, 2)
        self.assertFalse(report["ok"])
        self.assertEqual(report["error"]["code"], "PAID_CALL_ACK_REQUIRED")

    def test_real_mode_requires_explicit_local_env_opt_in_after_ack(self) -> None:
        completed, report = self.run_cli(
            "--execute-real",
            "--acknowledge-paid-calls",
            "MAX_16_PROVIDER_CALLS",
        )

        self.assertEqual(completed.returncode, 2)
        self.assertFalse(report["ok"])
        self.assertEqual(report["error"]["code"], "REAL_ENV_OPT_IN_REQUIRED")

    def test_budget_wrapper_forwards_structured_generation(self) -> None:
        class StructuredProvider:
            provider_id = "deepseek"

            def __init__(self) -> None:
                self.normal_calls = 0
                self.json_calls = 0

            def generate(self, **_kwargs: str) -> ProviderResponse:
                self.normal_calls += 1
                return ProviderResponse(ok=True, provider="deepseek", content="normal")

            def generate_json(self, **_kwargs: str) -> ProviderResponse:
                self.json_calls += 1
                return ProviderResponse(ok=True, provider="deepseek", content='{"ok":true}')

        delegate = StructuredProvider()
        ledger = CallLedger(mode="dry-run", max_calls=2)
        provider = BudgetedProvider(delegate, ledger, external=False)

        response = provider.generate_json(
            instructions="会议产物整理器，只输出 JSON",
            input_text="会议记录",
        )

        self.assertTrue(response.ok)
        self.assertEqual(delegate.json_calls, 1)
        self.assertEqual(delegate.normal_calls, 0)
        self.assertEqual(ledger.summary()["by_kind"], {"artifact": 1})


if __name__ == "__main__":
    unittest.main()
