from __future__ import annotations

import re
from pathlib import Path
import unittest

from scripts import bootstrap_ai_collaboration_studio as bootstrap


class PythonLockContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1]
        self.lock_path = self.root / "requirements-lock-win-py314.txt"

    def test_lock_is_exact_hashed_and_matches_the_verified_resolution(self) -> None:
        lines = [
            line.strip()
            for line in self.lock_path.read_text(encoding="ascii").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        expected = {
            "futu-api": "10.10.7008",
            "numpy": "2.5.2",
            "pandas": "3.0.5",
            "protobuf": "7.36.0",
            "pycryptodome": "3.23.0",
            "pypdf": "6.16.2",
            "python-dateutil": "2.9.0.post0",
            "simplejson": "4.1.1",
            "six": "1.17.0",
            "tzdata": "2026.3",
        }
        parsed = bootstrap.parse_hashed_lock(self.lock_path)

        self.assertEqual(parsed, expected)
        self.assertEqual(len(lines), len(expected))
        self.assertTrue(
            all(
                re.fullmatch(
                    r"[A-Za-z0-9_.-]+==[^\s]+ --hash=sha256:[0-9a-f]{64}",
                    line,
                )
                for line in lines
            )
        )

    def test_bootstrap_requires_hashes_and_exact_freeze_identity(self) -> None:
        source = (
            self.root / "scripts" / "bootstrap_ai_collaboration_studio.py"
        ).read_text(encoding="utf-8")

        self.assertIn('"--require-hashes"', source)
        self.assertIn('"requirements-lock-win-py314.txt"', source)
        self.assertIn("frozen_resolution != locked_requirements", source)
        self.assertTrue(bootstrap.python_lock_profile()["compatible"])


class CIWorkflowContractTests(unittest.TestCase):
    EXPECTED_ACTIONS = {
        "actions/checkout": "11d5960a326750d5838078e36cf38b85af677262",
        "actions/setup-python": "a26af69be951a213d495a4c3e4e4022e16d87065",
        "actions/setup-node": "49933ea5288caeca8642d1e84afbd3f7d6820020",
        "actions/upload-artifact": "ea165f8d65b6e75b540449e92b4886f43607fa02",
    }

    def test_workflow_uses_only_guarded_delivery_entrypoints(self) -> None:
        workflow = (
            Path(__file__).resolve().parents[1]
            / ".github"
            / "workflows"
            / "isolated-validation.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("runs-on: windows-latest", workflow)
        self.assertIn('python-version: "3.14"', workflow)
        self.assertIn('node-version: "24"', workflow)
        self.assertIn("defaults:\n      run:\n        shell: pwsh", workflow)
        self.assertIn("working-directory: ai_collaboration_studio", workflow)
        self.assertIn(
            "cache-dependency-path: ai_collaboration_studio/frontend/package-lock.json",
            workflow,
        )
        self.assertIn("AI_STUDIO_SKIP_LOCAL_ENV", workflow)
        self.assertIn("scripts/bootstrap_ai_collaboration_studio.py", workflow)
        self.assertIn("scripts/run_static_security_checks.py", workflow)
        self.assertIn("static-security.json", workflow)
        self.assertIn("Resolve guarded bootstrap runtime", workflow)
        self.assertIn("[System.IO.Path]::GetTempPath()", workflow)
        self.assertIn("AI_STUDIO_BOOTSTRAP_ROOT=$runtimeRoot", workflow)
        self.assertIn(
            '--runtime-root "$env:AI_STUDIO_BOOTSTRAP_ROOT"',
            workflow,
        )
        self.assertEqual(workflow.count("$env:AI_STUDIO_BOOTSTRAP_ROOT"), 5)
        self.assertNotIn("$env:RUNNER_TEMP\\ai-studio-bootstrap", workflow)
        self.assertIn("npm.cmd --prefix frontend test", workflow)
        self.assertIn("scripts/run_backend_tests_isolated.py", workflow)
        self.assertIn("--layer full", workflow)
        self.assertIn("scripts/run_fresh_source_smoke.py", workflow)
        self.assertIn("scripts/run_isolated_release_drill.py", workflow)
        self.assertIn("release-drill.json", workflow)
        self.assertIn("scripts/generate_dependency_inventory.py", workflow)
        self.assertIn("dependency-inventory.json", workflow)
        self.assertIn('--verify "$env:RUNNER_TEMP\\dependency-inventory.json"', workflow)
        self.assertEqual(workflow.count("--allow-dependency-downloads"), 2)
        self.assertNotIn("node --test", workflow)
        self.assertNotIn("python -m unittest", workflow)
        self.assertNotIn("server.py", workflow)
        self.assertNotIn("8770", workflow)

    def test_repository_root_launcher_delegates_to_the_guarded_launcher(self) -> None:
        root_launcher = (
            Path(__file__).resolve().parents[1]
            / "delivery"
            / "repository-root"
            / "run_ai_collaboration_studio.cmd.template"
        ).read_text(encoding="utf-8")

        self.assertIn("scripts\\start_ai_collaboration_studio.ps1", root_launcher)
        self.assertIn("AI_STUDIO_EXIT_CODE=%ERRORLEVEL%", root_launcher)
        self.assertTrue(root_launcher.isascii())
        self.assertNotIn("chcp", root_launcher.lower())
        self.assertNotIn("/api/health", root_launcher)
        self.assertNotIn("server.py", root_launcher)
        self.assertNotIn("Stop-Process", root_launcher)

    def test_every_action_is_pinned_to_the_reviewed_full_commit(self) -> None:
        workflow = (
            Path(__file__).resolve().parents[1]
            / ".github"
            / "workflows"
            / "isolated-validation.yml"
        ).read_text(encoding="utf-8")
        references = re.findall(
            r"^\s*uses:\s*([^\s#]+)",
            workflow,
            flags=re.MULTILINE,
        )
        parsed = {}
        for reference in references:
            action, separator, revision = reference.rpartition("@")
            self.assertEqual(separator, "@")
            self.assertRegex(revision, r"^[0-9a-f]{40}$")
            self.assertNotIn(action, parsed)
            parsed[action] = revision

        self.assertEqual(parsed, self.EXPECTED_ACTIONS)

    def test_workflow_is_read_only_by_default_and_redacts_provider_inputs(self) -> None:
        workflow = (
            Path(__file__).resolve().parents[1]
            / ".github"
            / "workflows"
            / "isolated-validation.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("permissions:\n  contents: read", workflow)
        for name in (
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "DEEPSEEK_API_KEY",
            "GOOGLE_API_KEY",
            "GEMINI_API_KEY",
            "FUTU_PASSWORD_MD5",
        ):
            self.assertRegex(workflow, rf"{name}: \"\"")
        self.assertIn(
            "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02",
            workflow,
        )
        self.assertNotIn("secrets.", workflow)


if __name__ == "__main__":
    unittest.main()
