from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from scripts import run_static_security_checks as security


class StaticSecurityCheckTests(unittest.TestCase):
    def test_current_publishable_source_passes_offline_baseline(self) -> None:
        root = Path(__file__).resolve().parents[1]
        result = security.run_security_checks(root)

        self.assertTrue(result["ok"], json.dumps(result, indent=2))
        self.assertEqual(result["schema_version"], "static_security_scan_v1")
        self.assertEqual(result["summary"]["checks"], 7)
        self.assertEqual(result["summary"]["failed"], 0)
        passive = next(
            check
            for check in result["checks"]
            if check["id"] == "passive_protected_port_inspection"
        )
        self.assertTrue(passive["ok"], passive)
        self.assertEqual(passive["evidence"]["release_active_socket_calls"], 0)
        self.assertTrue(result["boundaries"]["offline"])
        self.assertEqual(result["boundaries"]["network_requests"], 0)
        self.assertFalse(result["boundaries"]["sast_complete"])
        self.assertFalse(result["boundaries"]["dependency_cve_audit"])

    def test_secret_scan_reports_location_without_echoing_secret(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ai-studio-secret-scan-") as temp_dir:
            root = Path(temp_dir)
            source = root / "backend" / "leaked.py"
            source.parent.mkdir(parents=True)
            source.write_text(
                "material = '''-----BEGIN "
                + "PRIVATE KEY-----\nsensitive-material\n-----END'''\n",
                encoding="utf-8",
            )

            findings, scanned = security.find_high_confidence_secrets(root)

        self.assertEqual(scanned, 1)
        self.assertEqual(findings[0]["rule"], "private_key_block")
        self.assertEqual(findings[0]["path"], "backend/leaked.py")
        self.assertNotIn("sensitive-material", json.dumps(findings))

    def test_workflow_checks_reject_moving_action_and_direct_test_entry(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ai-studio-workflow-scan-") as temp_dir:
            root = Path(temp_dir)
            workflow = root / ".github" / "workflows" / "unsafe.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text(
                "jobs:\n"
                "  unsafe:\n"
                "    steps:\n"
                "      - uses: actions/checkout@v4\n"
                "      - run: node --test tests/example.test.js\n",
                encoding="utf-8",
            )

            pin_check = security.check_workflow_action_pins(root)
            entrypoint_check = security.check_isolated_ci_entrypoints(root)

        self.assertFalse(pin_check["ok"])
        self.assertEqual(
            pin_check["evidence"]["issues"][0]["rule"],
            "EXTERNAL_ACTION_NOT_FULL_SHA",
        )
        self.assertFalse(entrypoint_check["ok"])
        self.assertIn(
            "DIRECT_NODE_TEST",
            entrypoint_check["evidence"]["forbidden_rules"],
        )

    def test_protected_port_check_rejects_active_or_incomplete_probes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ai-studio-port-scan-") as temp_dir:
            root = Path(temp_dir)
            scripts = root / "scripts"
            scripts.mkdir()
            (scripts / "run_fresh_source_smoke.py").write_text(
                "def protected_port_state():\n"
                "    return {}\n\n"
                "def port_open(port):\n"
                "    return False\n\n"
                "def run_smoke():\n"
                "    return {str(protected): port_open(protected) "
                "for protected in (8770, 11111, 18787)}\n",
                encoding="utf-8",
            )
            (scripts / "run_isolated_release_drill.py").write_text(
                "def _protected_port_state():\n"
                "    return {}\n\n"
                "def run_drill():\n"
                "    socket.create_connection(('127.0.0.1', 8770))\n"
                "    return _protected_port_state()\n",
                encoding="utf-8",
            )

            result = security.check_passive_protected_port_inspection(root)

        self.assertFalse(result["ok"])
        self.assertIn(
            "RELEASE_ACTIVE_SOCKET_PROBE",
            result["evidence"]["issues"],
        )
        self.assertIn(
            "FRESH_RANDOM_PORT_PROBE_DRIFT",
            result["evidence"]["issues"],
        )
        self.assertIn(
            "FRESH_WINDOWS_IP_HELPER_MISSING",
            result["evidence"]["issues"],
        )
        self.assertIn(
            "RELEASE_PASSIVE_CHECK_COVERAGE_INCOMPLETE",
            result["evidence"]["issues"],
        )


if __name__ == "__main__":
    unittest.main()
