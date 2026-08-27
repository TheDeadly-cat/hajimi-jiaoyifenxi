from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"

from backend.decision_lineage import canonical_sha256
from backend.secure_mcp_tunnel_preflight import (
    SecureMCPTunnelPreflightError,
    evaluate_secure_mcp_tunnel_preflight,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def evidence(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "version": "secure_mcp_tunnel_preflight_evidence_v1",
        "tunnel_id_available": True,
        "runtime_api_key_available": True,
        "platform_permissions": {"read": True, "use": True, "manage": False},
        "target_chatgpt_workspace_associated": True,
        "chatgpt_developer_mode_enabled": True,
        "outbound_https_available": True,
        "tunnel_client_available": True,
        "local_mcp_reachable": True,
        "doctor": {
            "executed": True,
            "healthy": True,
            "ready": True,
            "connected": True,
        },
    }
    value.update(overrides)
    return value


class SecureMCPTunnelPreflightTests(unittest.TestCase):
    def test_existing_tunnel_can_be_runtime_ready_without_manage_permission(self) -> None:
        supplied = evidence()
        report = evaluate_secure_mcp_tunnel_preflight(supplied)
        self.assertEqual(report["missing_or_unverified"], [])
        self.assertEqual(report["evidence_sha256"], canonical_sha256(supplied))
        self.assertTrue(report["readiness"]["ready_to_run_doctor"])
        self.assertTrue(
            report["readiness"]["runtime_connection_verified_by_declared_evidence"]
        )
        self.assertFalse(report["tunnel_administration"]["manage_permission_declared"])
        self.assertFalse(report["tunnel_administration"]["required_to_use_existing_tunnel"])
        self.assertFalse(report["readiness"]["external_permission_truth_verified"])
        self.assertEqual(report["next_step"]["id"], "test_chatgpt_developer_app")
        self.assertEqual(report["safety"]["network_calls_performed"], 0)
        report_hash = report.pop("report_sha256")
        self.assertEqual(report_hash, canonical_sha256(report))

    def test_missing_layers_remain_distinct_and_fail_closed(self) -> None:
        report = evaluate_secure_mcp_tunnel_preflight(evidence(
            tunnel_id_available=False,
            runtime_api_key_available=False,
            platform_permissions={"read": True, "use": False, "manage": True},
            target_chatgpt_workspace_associated=False,
            chatgpt_developer_mode_enabled=False,
            outbound_https_available=False,
            tunnel_client_available=False,
            local_mcp_reachable=False,
            doctor={
                "executed": False,
                "healthy": False,
                "ready": False,
                "connected": False,
            },
        ))
        self.assertEqual(
            report["missing_or_unverified"],
            [
                "platform_tunnels_use",
                "tunnel_id_available",
                "runtime_api_key_available",
                "target_chatgpt_workspace_associated",
                "chatgpt_developer_mode_enabled",
                "tunnel_client_available",
                "outbound_https_available",
                "local_mcp_reachable",
            ],
        )
        self.assertFalse(report["readiness"]["ready_to_run_doctor"])
        self.assertFalse(
            report["readiness"]["runtime_connection_verified_by_declared_evidence"]
        )
        self.assertEqual(report["next_step"]["id"], "resolve_declared_prerequisites")

    def test_closed_schema_and_doctor_semantics_reject_ambiguous_evidence(self) -> None:
        unknown = evidence(unexpected=True)
        with self.assertRaises(SecureMCPTunnelPreflightError):
            evaluate_secure_mcp_tunnel_preflight(unknown)

        with self.assertRaises(SecureMCPTunnelPreflightError):
            evaluate_secure_mcp_tunnel_preflight(evidence(
                doctor={
                    "executed": False,
                    "healthy": True,
                    "ready": False,
                    "connected": False,
                },
            ))

        with self.assertRaises(SecureMCPTunnelPreflightError):
            evaluate_secure_mcp_tunnel_preflight(evidence(
                doctor={
                    "executed": True,
                    "healthy": False,
                    "ready": False,
                    "connected": True,
                },
            ))

    def test_cli_reads_once_without_modifying_or_disclosing_the_input_path(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="ai-studio-tunnel-preflight-",
            ignore_cleanup_errors=True,
        ) as temp_dir:
            evidence_path = Path(temp_dir) / "evidence.json"
            evidence_path.write_text(json.dumps(evidence()), encoding="utf-8")
            before = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "backend.secure_mcp_tunnel_preflight",
                    "--evidence-file",
                    str(evidence_path),
                ],
                cwd=PROJECT_ROOT,
                env={**os.environ, "AI_STUDIO_SKIP_LOCAL_ENV": "1"},
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            after = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        self.assertEqual(after, before)
        self.assertNotIn(str(evidence_path), completed.stdout)
        report = json.loads(completed.stdout)
        self.assertFalse(report["safety"]["secret_values_accepted"])
        self.assertFalse(report["safety"]["local_mcp_server_started"])


if __name__ == "__main__":
    unittest.main()
