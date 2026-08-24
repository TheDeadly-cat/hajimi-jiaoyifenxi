from __future__ import annotations

import unittest
from pathlib import Path


QA_SCRIPT_NAMES = (
    "run_isolated_action_desk_qa.py",
    "run_isolated_candidate_experiment_qa.py",
    "run_isolated_project_readiness_qa.py",
    "run_isolated_project_round_focus_qa.py",
)


class IsolatedQAScriptContractTests(unittest.TestCase):
    def test_all_qa_scripts_compile_and_set_isolated_boundaries_before_backend_import(self) -> None:
        scripts_dir = Path(__file__).resolve().parents[1] / "scripts"
        for script_name in QA_SCRIPT_NAMES:
            with self.subTest(script=script_name):
                script_path = scripts_dir / script_name
                source = script_path.read_text(encoding="utf-8")
                compile(source, str(script_path), "exec")

                env_marker = 'os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"'
                backend_import = source.find("from backend")
                self.assertGreaterEqual(source.find(env_marker), 0)
                self.assertTrue(
                    backend_import < 0 or source.find(env_marker) < backend_import,
                    "backend imports must occur after local-env isolation",
                )
                self.assertIn("AI_STUDIO_RUNTIME_DIR", source)
                self.assertIn("AI_STUDIO_DATABASE_PATH", source)
                self.assertIn("ThreadingHTTPServer((\"127.0.0.1\", 0)", source)
                self.assertTrue(
                    "server.server_port == 8770" in source
                    or 'os.environ["AI_STUDIO_PORT"] == "8770"' in source,
                    "ephemeral QA must explicitly reject the formal port",
                )

    def test_action_desk_ready_file_stays_in_system_temp(self) -> None:
        script_path = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "run_isolated_action_desk_qa.py"
        )
        source = script_path.read_text(encoding="utf-8")
        self.assertIn('AI_STUDIO_QA_READY_FILE', source)
        self.assertIn('Path(tempfile.gettempdir()).resolve()', source)
        self.assertIn('ready_path.relative_to(temp_parent)', source)

    def test_candidate_experiment_ready_file_stays_in_system_temp(self) -> None:
        script_path = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "run_isolated_candidate_experiment_qa.py"
        )
        source = script_path.read_text(encoding="utf-8")
        self.assertIn('AI_STUDIO_QA_READY_FILE', source)
        self.assertIn('Path(tempfile.gettempdir()).resolve()', source)
        self.assertIn('ready_path.relative_to(temp_parent)', source)

    def test_project_round_focus_qa_uses_the_generic_v5_context_contract(self) -> None:
        script_path = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "run_isolated_project_round_focus_qa.py"
        )
        source = script_path.read_text(encoding="utf-8")
        self.assertIn('"round_launch_plan_v5"', source)
        self.assertIn('"round_context_authorization_set_v1"', source)
        self.assertIn('"round_context_authorization_entry_v1"', source)
        self.assertIn('round_context_authorizations=authorization_set', source)
        self.assertIn('"v5_round_context_plan_verified": True', source)
        self.assertIn("flush=True", source)
        self.assertNotIn("def _assert_v4_plan", source)
        self.assertNotIn('"v4_plan_verified"', source)
