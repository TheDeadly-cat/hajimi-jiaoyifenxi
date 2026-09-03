from __future__ import annotations

import inspect
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


os.environ.setdefault("AI_STUDIO_SKIP_LOCAL_ENV", "1")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend import source_monitoring_acceptance_cli as cli  # noqa: E402


class SourceMonitoringAcceptanceCliTests(unittest.TestCase):
    def run_main(self, argv: list[str]) -> tuple[int, dict[str, object]]:
        output = io.StringIO()
        code = cli.main(argv, output=output)
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        return code, json.loads(lines[0])

    def test_help_is_zero_action_and_keeps_broad_acceptance_unclaimed(self) -> None:
        code, payload = self.run_main(["--help"])
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["overall_acceptance"], "NOT_CLAIMED")
        self.assertFalse(payload["content_truth_attested"])
        self.assertFalse(payload["independent_network_witness"])
        self.assertEqual(payload["safety"]["database_reads_performed"], 0)
        self.assertEqual(payload["safety"]["network_requests_performed"], 0)

    def test_bad_arguments_and_duplicate_bundle_fail_closed(self) -> None:
        for argv in (
            [],
            ["verify"],
            ["verify", "--bundle", "a", "--bundle", "b"],
            ["verify", "--bundle=a", "--bundle=b"],
            ["verify", "--bundle", "a", "--bundle=b"],
            ["verify", "--bundle=a", "--bundle", "b"],
            ["verify", "--bundle="],
            ["verify", "--bundle", ""],
            ["verify", "--bundle", " "],
            ["verify", "--bund", "a"],
            ["start", "--bundle", "a"],
        ):
            with self.subTest(argv=argv):
                code, payload = self.run_main(list(argv))
                self.assertEqual(code, 2)
                self.assertFalse(payload["ok"])
                self.assertEqual(
                    payload["error_code"],
                    "SOURCE_MONITORING_ACCEPTANCE_ARGUMENT_INVALID",
                )
                self.assertEqual(payload["overall_acceptance"], "NOT_CLAIMED")

    def test_invalid_bundle_is_bounded_and_does_not_echo_path(self) -> None:
        wrapper = PROJECT_ROOT / "scripts" / "run_source_monitoring_acceptance.py"
        marker = "SECRET_ACCEPTANCE_BUNDLE_PATH"
        for bundle_arguments in (["--bundle", marker], [f"--bundle={marker}"]):
            with self.subTest(bundle_arguments=bundle_arguments):
                with tempfile.TemporaryDirectory() as temp_dir:
                    environment = dict(os.environ)
                    environment["PYTHONPATH"] = ""
                    environment["PYTHONNOUSERSITE"] = "1"
                    completed = subprocess.run(
                        [
                            sys.executable,
                            "-I",
                            "-B",
                            str(wrapper),
                            "verify",
                            *bundle_arguments,
                        ],
                        cwd=temp_dir,
                        env=environment,
                        capture_output=True,
                        text=True,
                        timeout=30,
                        check=False,
                    )
                self.assertEqual(completed.returncode, 2, completed.stderr)
                lines = completed.stdout.splitlines()
                self.assertEqual(len(lines), 1)
                payload = json.loads(lines[0])
                self.assertEqual(payload["source_acceptance_verdict"], "FAIL")
                self.assertNotEqual(
                    payload.get("error_code"),
                    "SOURCE_MONITORING_ACCEPTANCE_ARGUMENT_INVALID",
                )
                self.assertNotIn(marker, completed.stdout)
                self.assertLessEqual(
                    len(lines[0].encode("utf-8")),
                    cli.MAX_SOURCE_MONITORING_ACCEPTANCE_OUTPUT_BYTES,
                )
                self.assertEqual(completed.stderr, "")

    def test_public_main_has_no_verifier_or_path_injection_parameter(self) -> None:
        self.assertEqual(
            list(inspect.signature(cli.main).parameters),
            ["argv", "output"],
        )

    def test_valid_verify_forms_require_isolation_before_verifier_import(self) -> None:
        verifier_module = "backend.source_monitoring.soak_acceptance"
        previously_loaded = sys.modules.pop(verifier_module, None)
        try:
            for argv in (
                ["verify", "--bundle", "missing"],
                ["verify", "--bundle=missing"],
            ):
                with self.subTest(argv=argv):
                    code, payload = self.run_main(list(argv))
                    self.assertEqual(code, 2)
                    self.assertEqual(
                        payload["error_code"],
                        "SOURCE_MONITORING_ACCEPTANCE_ISOLATED_PROCESS_REQUIRED",
                    )
            self.assertNotIn(verifier_module, sys.modules)
        finally:
            if previously_loaded is not None:
                sys.modules[verifier_module] = previously_loaded

    def test_wrapper_help_runs_in_fresh_isolated_process(self) -> None:
        wrapper = PROJECT_ROOT / "scripts" / "run_source_monitoring_acceptance.py"
        with tempfile.TemporaryDirectory() as temp_dir:
            environment = dict(os.environ)
            environment["PYTHONPATH"] = ""
            environment["PYTHONNOUSERSITE"] = "1"
            completed = subprocess.run(
                [sys.executable, "-I", "-B", str(wrapper), "--help"],
                cwd=temp_dir,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["command"], "help")
        self.assertEqual(payload["overall_acceptance"], "NOT_CLAIMED")
        self.assertEqual(completed.stderr, "")

    def test_wrapper_non_isolated_verify_is_zero_action_before_backend_import(
        self,
    ) -> None:
        source_wrapper = (
            PROJECT_ROOT / "scripts" / "run_source_monitoring_acceptance.py"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            temporary_root = Path(temp_dir)
            copied_wrapper = temporary_root / "scripts" / source_wrapper.name
            copied_wrapper.parent.mkdir()
            copied_wrapper.write_text(
                source_wrapper.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            backend_dir = temporary_root / "backend"
            backend_dir.mkdir()
            (backend_dir / "__init__.py").write_text("", encoding="utf-8")
            (backend_dir / "source_monitoring_acceptance_cli.py").write_text(
                "import os\n"
                "from pathlib import Path\n"
                "Path(os.environ['ACCEPTANCE_IMPORT_SENTINEL']).write_text("
                "'imported', encoding='utf-8')\n"
                "def main():\n"
                "    return 0\n",
                encoding="utf-8",
            )
            sentinel = temporary_root / "backend-imported.txt"
            environment = dict(os.environ)
            environment["PYTHONPATH"] = ""
            environment["PYTHONNOUSERSITE"] = "1"
            environment["ACCEPTANCE_IMPORT_SENTINEL"] = str(sentinel)
            completed = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(copied_wrapper),
                    "verify",
                    "--bundle",
                    "SECRET_NON_ISOLATED_BUNDLE",
                ],
                cwd=temporary_root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            sentinel_exists = sentinel.exists()

        self.assertEqual(completed.returncode, 2, completed.stderr)
        lines = completed.stdout.splitlines()
        self.assertEqual(len(lines), 1)
        payload = json.loads(lines[0])
        self.assertEqual(
            payload["error_code"],
            "SOURCE_MONITORING_ACCEPTANCE_ISOLATED_PROCESS_REQUIRED",
        )
        self.assertEqual(payload["overall_acceptance"], "NOT_CLAIMED")
        self.assertEqual(payload["safety"]["database_reads_performed"], 0)
        self.assertEqual(payload["safety"]["network_requests_performed"], 0)
        self.assertNotIn("SECRET_NON_ISOLATED_BUNDLE", completed.stdout)
        self.assertLessEqual(
            len(lines[0].encode("utf-8")),
            cli.MAX_SOURCE_MONITORING_ACCEPTANCE_OUTPUT_BYTES,
        )
        self.assertFalse(sentinel_exists)
        self.assertEqual(completed.stderr, "")


if __name__ == "__main__":
    unittest.main()
