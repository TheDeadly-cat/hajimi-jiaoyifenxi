from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from scripts import bootstrap_ai_collaboration_studio as bootstrap
from scripts import run_fresh_source_smoke as fresh_smoke


class DeliveryBootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="ai-studio-bootstrap-contract-"
        )
        self.temp_path = Path(self.temp_dir.name)
        self.project = self.temp_path / "project"
        frontend = self.project / "frontend"
        frontend.mkdir(parents=True)
        (self.project / "server.py").write_text("pass\n", encoding="utf-8")
        (self.project / "requirements.txt").write_text(
            "example>=1,<2\n",
            encoding="utf-8",
        )
        (self.project / "requirements-lock-win-py314.txt").write_text(
            "example==1.0 --hash=sha256:" + ("0" * 64) + "\n",
            encoding="ascii",
        )
        (frontend / "package.json").write_text(
            json.dumps(
                {
                    "name": "ai-collaboration-studio",
                    "version": "0.1.0",
                }
            ),
            encoding="utf-8",
        )
        (frontend / "package-lock.json").write_text("{}\n", encoding="utf-8")
        self.bootstrap_script = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "bootstrap_ai_collaboration_studio.py"
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def run_bootstrap(self, *extra: str) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"
        return subprocess.run(
            [
                sys.executable,
                str(self.bootstrap_script),
                "--project-root",
                str(self.project),
                *extra,
            ],
            cwd=self.project,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )

    def test_check_only_reports_inputs_without_creating_runtime(self) -> None:
        runtime = self.temp_path / "check-runtime"
        completed = self.run_bootstrap(
            "--runtime-root",
            str(runtime),
            "--check-only",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["check_only"])
        self.assertFalse(payload["dependency_downloads_authorized"])
        self.assertTrue(payload["inputs"]["python_requirements_fully_pinned"])
        self.assertEqual(payload["inputs"]["python_locked_distribution_count"], 1)
        self.assertTrue(
            payload["inputs"]["python_lock_profile"]["compatible"]
        )
        self.assertFalse(runtime.exists())

    def test_download_authorization_is_required_before_runtime_write(self) -> None:
        runtime = self.temp_path / "unauthorized-runtime"
        completed = self.run_bootstrap(
            "--runtime-root",
            str(runtime),
        )

        self.assertEqual(completed.returncode, 1)
        payload = json.loads(completed.stderr)
        self.assertIn("--allow-dependency-downloads", payload["error"])
        self.assertFalse(runtime.exists())

    def test_runtime_inside_source_must_use_project_runtime_boundary(self) -> None:
        project = bootstrap.resolve_project_root(self.project)
        with self.assertRaisesRegex(
            bootstrap.BootstrapError,
            "must remain under project/runtime",
        ):
            bootstrap.resolve_runtime_root(
                project,
                self.project / "frontend" / "bootstrap-cache",
            )

    def test_runtime_root_does_not_trust_runner_temp_environment(self) -> None:
        project = bootstrap.resolve_project_root(self.project)
        system_temp = Path(tempfile.gettempdir()).resolve()
        outside = system_temp.parent / "ai-studio-untrusted-runner-temp" / "runtime"

        with patch.dict(
            os.environ,
            {"GITHUB_ACTIONS": "true", "RUNNER_TEMP": str(outside.parent)},
            clear=False,
        ):
            with self.assertRaisesRegex(
                bootstrap.BootstrapError,
                "system temp directory",
            ):
                bootstrap.resolve_runtime_root(project, outside)

    def test_safe_extract_rejects_parent_traversal(self) -> None:
        archive = self.temp_path / "unsafe.zip"
        with ZipFile(archive, "w") as output:
            output.writestr("../escaped.txt", "escape")

        with self.assertRaisesRegex(fresh_smoke.SmokeError, "escapes destination"):
            fresh_smoke.safe_extract(archive, self.temp_path / "extract")
        self.assertFalse((self.temp_path / "escaped.txt").exists())

    def test_application_environment_removes_credentials_and_proxies(self) -> None:
        environment = fresh_smoke.isolated_app_environment(
            {
                "OPENAI_API_KEY": "secret",
                "HTTP_PROXY": "http://proxy.invalid",
                "FUTU_HOST": "remote.invalid",
                "FUTU_PORT": "11111",
                "KEEP_ME": "yes",
            }
        )

        self.assertEqual(environment["AI_STUDIO_SKIP_LOCAL_ENV"], "1")
        self.assertEqual(environment["FUTU_HOST"], "127.0.0.1")
        self.assertEqual(environment["FUTU_PORT"], "1")
        self.assertEqual(environment["KEEP_ME"], "yes")
        self.assertNotIn("OPENAI_API_KEY", environment)
        self.assertNotIn("HTTP_PROXY", environment)

    def test_protected_port_state_uses_passive_listener_tables(self) -> None:
        with patch.object(
            fresh_smoke,
            "_windows_listener_ports",
            side_effect=[{8770}, {11111}],
        ) as listener_query:
            state = fresh_smoke.protected_port_state()

        self.assertEqual(
            state,
            {"8770": True, "11111": True, "18787": False},
        )
        self.assertEqual(listener_query.call_count, 2)

    def test_clean_source_projection_is_frozen_before_generated_state(self) -> None:
        source = self.temp_path / "clean-source"
        (source / "frontend").mkdir(parents=True)

        initial = fresh_smoke.clean_source_exclusion_snapshot(source)
        self.assertEqual(
            initial,
            {
                ".git": True,
                "runtime": True,
                "node_modules": True,
                "dist": True,
            },
        )
        (source / "frontend" / "node_modules").mkdir()
        self.assertFalse(
            fresh_smoke.clean_source_exclusion_snapshot(source)["node_modules"]
        )

    def test_test_summary_accumulates_safe_runner_files_and_unittest(self) -> None:
        frontend = (
            "\u2139 tests 2\n\u2139 pass 2\n\u2139 fail 0\n"
            "\u2139 tests 3\n\u2139 pass 3\n\u2139 fail 0\n"
        )
        self.assertEqual(
            fresh_smoke.parse_test_summary(frontend),
            {"tests": 5, "pass": 5, "fail": 0},
        )
        self.assertEqual(
            fresh_smoke.parse_test_summary(
                "Ran 6 tests in 1.000s\n\nOK\n"
            ),
            {"tests": 6, "pass": 6, "fail": 0},
        )


if __name__ == "__main__":
    unittest.main()
