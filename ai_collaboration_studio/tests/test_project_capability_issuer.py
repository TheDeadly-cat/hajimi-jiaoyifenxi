from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from backend.project_capability_issuer import (
    PROJECT_CAPABILITY_ISSUANCE_ACKNOWLEDGEMENT,
    PROJECT_CAPABILITY_ISSUANCE_OUTPUT_VERSION,
    PROJECT_CAPABILITY_ISSUANCE_RECEIPT_VERSION,
    PROJECT_CAPABILITY_ISSUER_POLICY_VERSION,
    ProjectCapabilityIssuerError,
    inspect_project_capability_request,
    issue_project_capability,
    normalize_project_capability_issuer_policy,
    plan_project_capability_issuance,
)
from backend.project_invocation import (
    PROJECT_INVOCATION_ACTION_INTAKE,
    PROJECT_INVOCATION_ACTION_RESULT_READ,
    ProjectCapabilityAuthorizer,
    derive_project_invocation_room_id,
    seal_project_invocation_envelope,
)


SECRET = "offline-issuer-test-secret-at-least-32-bytes"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def envelope() -> dict[str, object]:
    caller_id = "caller_alpha"
    project_id = "project_public_fixture"
    client_request_id = "request-issuer-0001"
    return seal_project_invocation_envelope({
        "version": "project_invocation_envelope_v1",
        "caller_id": caller_id,
        "project_id": project_id,
        "client_request_id": client_request_id,
        "room_id": derive_project_invocation_room_id(
            caller_id,
            project_id,
            client_request_id,
        ),
        "source": {"item_id": "fixture_001", "revision": "1"},
        "workflow_kind": "research",
        "result_profile": "research_report_v1",
        "room_spec": {
            "title": "Public fixture",
            "objective": "Validate the bounded cross-project workflow.",
            "domain": "project_research",
            "category": "Project research",
            "template_id": "open_collaboration",
            "capability_pack_ids": [],
        },
        "domain_context": {
            "schema_version": "public_fixture_v1",
            "schema_sha256": "1" * 64,
            "payload_sha256": "2" * 64,
        },
        "input_manifest": {
            "content_sha256": "3" * 64,
            "content_bytes": 1024,
        },
        "data_handling": {
            "classification": "public",
            "retention_policy": "no_payload_retention",
            "retention_days": None,
        },
        "budget": {
            "max_provider_calls": 0,
            "max_context_bytes": 100_000,
            "max_result_bytes": 200_000,
        },
        "user_confirmation": {
            "required": True,
            "boundary": "before_room_creation",
        },
        "safety": {
            "execution_capability": "none",
            "live_trading_allowed": False,
            "can_autonomously_decide": False,
        },
    })


def policy(*, enabled: bool = True) -> dict[str, object]:
    return {
        "version": PROJECT_CAPABILITY_ISSUER_POLICY_VERSION,
        "projects": [{
            "caller_id": "caller_alpha",
            "project_id": "project_public_fixture",
            "enabled": enabled,
            "allowed_actions": [
                PROJECT_INVOCATION_ACTION_INTAKE,
                PROJECT_INVOCATION_ACTION_RESULT_READ,
            ],
            "max_ttl_seconds": 300,
        }],
    }


class ProjectCapabilityIssuerContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.envelope = envelope()
        self.policy = policy()

    def test_inspect_and_dry_run_do_not_read_a_secret_or_mint(self) -> None:
        inspection = inspect_project_capability_request(
            self.envelope,
            self.policy,
        )
        self.assertEqual(inspection["status"], "validated")
        self.assertFalse(inspection["secret_read"])
        self.assertFalse(inspection["token_minted"])
        self.assertNotIn("title", inspection["envelope"])
        self.assertNotIn("objective", inspection["envelope"])
        self.assertEqual(
            inspection["envelope"]["request_sha256"],
            self.envelope["request_sha256"],
        )

        plan = plan_project_capability_issuance(
            self.envelope,
            self.policy,
            action=PROJECT_INVOCATION_ACTION_INTAKE,
            ttl_seconds=60,
        )
        self.assertEqual(plan["action"], PROJECT_INVOCATION_ACTION_INTAKE)
        self.assertEqual(plan["ttl_seconds"], 60)
        self.assertTrue(plan["scope"]["single_action"])
        self.assertFalse(
            plan["replay_semantics"]["single_use_consumption_enforced_by_host"]
        )
        self.assertTrue(plan["replay_semantics"]["intake_replay_is_idempotent"])
        self.assertFalse(plan["secret_read"])
        self.assertFalse(plan["token_minted"])

    def test_issue_reuses_existing_single_action_contract_and_safe_receipt(self) -> None:
        output = issue_project_capability(
            self.envelope,
            self.policy,
            action=PROJECT_INVOCATION_ACTION_RESULT_READ,
            ttl_seconds=45,
            signing_secret=SECRET,
            clock=lambda: 2_000_000_000,
        )
        self.assertEqual(output["version"], PROJECT_CAPABILITY_ISSUANCE_OUTPUT_VERSION)
        token = output["token"]
        receipt = output["receipt"]
        self.assertEqual(
            receipt["version"],
            PROJECT_CAPABILITY_ISSUANCE_RECEIPT_VERSION,
        )
        self.assertNotIn(SECRET, json.dumps(output, ensure_ascii=False))
        self.assertNotIn(token, json.dumps(receipt, ensure_ascii=False))
        self.assertFalse(receipt["signing_secret_in_receipt"])
        self.assertFalse(receipt["bearer_token_in_receipt"])
        self.assertEqual(receipt["issued_at"], 2_000_000_000)
        self.assertEqual(receipt["expires_at"], 2_000_000_045)

        claims = ProjectCapabilityAuthorizer(
            SECRET,
            clock=lambda: 2_000_000_000,
        ).authorize(
            token,
            caller_id=self.envelope["caller_id"],
            project_id=self.envelope["project_id"],
            room_id=self.envelope["room_id"],
            action=PROJECT_INVOCATION_ACTION_RESULT_READ,
            client_request_id=self.envelope["client_request_id"],
            request_sha256=self.envelope["request_sha256"],
        )
        self.assertEqual(claims.actions, (PROJECT_INVOCATION_ACTION_RESULT_READ,))

    def test_unknown_disabled_denied_action_and_ttl_fail_closed(self) -> None:
        unknown = copy.deepcopy(self.envelope)
        unknown["project_id"] = "project_unknown"
        unknown["room_id"] = derive_project_invocation_room_id(
            unknown["caller_id"],
            unknown["project_id"],
            unknown["client_request_id"],
        )
        unknown.pop("request_sha256")
        unknown = seal_project_invocation_envelope(unknown)
        with self.assertRaises(ProjectCapabilityIssuerError) as unknown_error:
            inspect_project_capability_request(unknown, self.policy)
        self.assertEqual(
            unknown_error.exception.code,
            "PROJECT_CAPABILITY_ISSUER_PROJECT_UNKNOWN",
        )

        with self.assertRaises(ProjectCapabilityIssuerError) as disabled_error:
            inspect_project_capability_request(self.envelope, policy(enabled=False))
        self.assertEqual(
            disabled_error.exception.code,
            "PROJECT_CAPABILITY_ISSUER_PROJECT_DISABLED",
        )

        intake_only = policy()
        intake_only["projects"][0]["allowed_actions"] = [
            PROJECT_INVOCATION_ACTION_INTAKE
        ]
        with self.assertRaises(ProjectCapabilityIssuerError) as denied_error:
            plan_project_capability_issuance(
                self.envelope,
                intake_only,
                action=PROJECT_INVOCATION_ACTION_RESULT_READ,
                ttl_seconds=60,
            )
        self.assertEqual(
            denied_error.exception.code,
            "PROJECT_CAPABILITY_ISSUER_ACTION_DENIED",
        )

        with self.assertRaises(ProjectCapabilityIssuerError) as action_error:
            plan_project_capability_issuance(
                self.envelope,
                self.policy,
                action="project_invocation.all",
                ttl_seconds=60,
            )
        self.assertEqual(
            action_error.exception.code,
            "PROJECT_CAPABILITY_ISSUER_ACTION_UNKNOWN",
        )

        for ttl in (True, 0, 301, 901):
            with self.subTest(ttl=ttl):
                with self.assertRaises(ProjectCapabilityIssuerError) as ttl_error:
                    plan_project_capability_issuance(
                        self.envelope,
                        self.policy,
                        action=PROJECT_INVOCATION_ACTION_INTAKE,
                        ttl_seconds=ttl,
                    )
                self.assertEqual(
                    ttl_error.exception.code,
                    "PROJECT_CAPABILITY_ISSUER_TTL_DENIED",
                )

    def test_policy_is_closed_sorted_unique_and_rejects_broad_actions(self) -> None:
        extra = policy()
        extra["projects"][0]["scope"] = "all"
        with self.assertRaises(ProjectCapabilityIssuerError):
            normalize_project_capability_issuer_policy(extra)

        broad = policy()
        broad["projects"][0]["allowed_actions"] = ["project_invocation.all"]
        with self.assertRaises(ProjectCapabilityIssuerError):
            normalize_project_capability_issuer_policy(broad)

        invalid_identity = policy()
        invalid_identity["projects"][0]["caller_id"] = "未知调用方"
        with self.assertRaises(ProjectCapabilityIssuerError):
            normalize_project_capability_issuer_policy(invalid_identity)

        duplicate = policy()
        duplicate["projects"].append(copy.deepcopy(duplicate["projects"][0]))
        with self.assertRaises(ProjectCapabilityIssuerError):
            normalize_project_capability_issuer_policy(duplicate)


class ProjectCapabilityIssuerCLITests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.envelope_path = self.root / "envelope.json"
        self.policy_path = self.root / "policy.json"
        self.envelope_path.write_text(
            json.dumps(envelope(), ensure_ascii=False),
            encoding="utf-8",
        )
        self.policy_path.write_text(
            json.dumps(policy(), ensure_ascii=False),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_cli(
        self,
        *arguments: str,
        secret: str | None = None,
        environment_secret: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"
        environment.pop("AI_STUDIO_PROJECT_CAPABILITY_SIGNING_SECRET", None)
        if environment_secret is not None:
            environment["AI_STUDIO_PROJECT_CAPABILITY_SIGNING_SECRET"] = (
                environment_secret
            )
        return subprocess.run(
            [sys.executable, "-m", "backend.project_capability_issuer", *arguments],
            cwd=PROJECT_ROOT,
            env=environment,
            input=secret,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    def common(self) -> list[str]:
        return [
            "--envelope",
            str(self.envelope_path),
            "--policy",
            str(self.policy_path),
        ]

    def test_cli_inspect_and_dry_run_need_no_secret(self) -> None:
        inspected = self.run_cli("inspect", *self.common())
        self.assertEqual(inspected.returncode, 0, inspected.stderr)
        inspection = json.loads(inspected.stdout)
        self.assertFalse(inspection["secret_read"])

        dry_run = self.run_cli(
            "dry-run",
            *self.common(),
            "--action",
            PROJECT_INVOCATION_ACTION_INTAKE,
            "--ttl-seconds",
            "30",
        )
        self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
        plan = json.loads(dry_run.stdout)
        self.assertEqual(plan["status"], "ready_to_mint")
        self.assertNotIn("token", plan)

    def test_cli_mint_requires_exact_ack_and_secret_without_echoing_secret(self) -> None:
        missing_ack = self.run_cli(
            "mint",
            *self.common(),
            "--action",
            PROJECT_INVOCATION_ACTION_INTAKE,
            "--acknowledgement",
            "NO",
            environment_secret=SECRET,
        )
        self.assertEqual(missing_ack.returncode, 2)
        self.assertIn("ACKNOWLEDGEMENT_REQUIRED", missing_ack.stderr)
        self.assertNotIn(SECRET, missing_ack.stderr)

        missing_secret = self.run_cli(
            "mint",
            *self.common(),
            "--action",
            PROJECT_INVOCATION_ACTION_INTAKE,
            "--acknowledgement",
            PROJECT_CAPABILITY_ISSUANCE_ACKNOWLEDGEMENT,
        )
        self.assertEqual(missing_secret.returncode, 2)
        self.assertIn("SECRET_MISSING", missing_secret.stderr)

        minted = self.run_cli(
            "mint",
            *self.common(),
            "--action",
            PROJECT_INVOCATION_ACTION_INTAKE,
            "--ttl-seconds",
            "30",
            "--acknowledgement",
            PROJECT_CAPABILITY_ISSUANCE_ACKNOWLEDGEMENT,
            environment_secret=SECRET,
        )
        self.assertEqual(minted.returncode, 0, minted.stderr)
        self.assertNotIn(SECRET, minted.stdout)
        payload = json.loads(minted.stdout)
        self.assertTrue(payload["token"])
        self.assertNotIn(payload["token"], json.dumps(payload["receipt"]))

    def test_cli_can_read_secret_from_stdin_and_create_exclusive_output(self) -> None:
        output = self.root / "issued.json"
        minted = self.run_cli(
            "mint",
            *self.common(),
            "--action",
            PROJECT_INVOCATION_ACTION_RESULT_READ,
            "--ttl-seconds",
            "30",
            "--acknowledgement",
            PROJECT_CAPABILITY_ISSUANCE_ACKNOWLEDGEMENT,
            "--secret-stdin",
            "--output",
            str(output),
            secret=f"{SECRET}\n",
        )
        self.assertEqual(minted.returncode, 0, minted.stderr)
        self.assertEqual(minted.stdout, "")
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertTrue(payload["token"])
        self.assertNotIn(SECRET, output.read_text(encoding="utf-8"))

        second = self.run_cli(
            "inspect",
            *self.common(),
            "--output",
            str(output),
        )
        self.assertEqual(second.returncode, 2)
        self.assertIn("OUTPUT_EXISTS", second.stderr)

    def test_cli_rejects_duplicate_json_keys(self) -> None:
        self.policy_path.write_text(
            '{"version":"project_capability_issuer_policy_v1",'
            '"version":"project_capability_issuer_policy_v1","projects":[]}',
            encoding="utf-8",
        )
        result = self.run_cli("inspect", *self.common())
        self.assertEqual(result.returncode, 2)
        self.assertIn("JSON_INVALID", result.stderr)


if __name__ == "__main__":
    unittest.main()
