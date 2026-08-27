from __future__ import annotations

import json
import socket
import subprocess
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNNER = PROJECT_ROOT / "scripts" / "run_isolated_manual_chatgpt_qa.py"


class ManualChatGPTQARunnerTests(unittest.TestCase):
    def test_help_exits_without_starting_a_fixture(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(RUNNER), "--help"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("--lifetime-seconds", completed.stdout)
        self.assertIn("--initial-state", completed.stdout)
        self.assertNotIn('"url":', completed.stdout)

    def test_each_supported_initial_state_is_explicit_and_isolated(self) -> None:
        expected_states = {
            "bundle-ready": "BUNDLE_READY",
            "waiting": "WAITING_FOR_CHATGPT",
            "import-rejected": "IMPORT_REJECTED",
            "api-review": "API_REVIEW",
        }

        for requested, expected in expected_states.items():
            with self.subTest(initial_state=requested):
                completed = subprocess.run(
                    [
                        sys.executable,
                        str(RUNNER),
                        "--lifetime-seconds",
                        "1",
                        "--keep-open-after-frozen",
                        "--initial-state",
                        requested,
                    ],
                    cwd=PROJECT_ROOT,
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                )

                self.assertEqual(completed.returncode, 0, completed.stderr)
                payload = json.loads(completed.stdout.splitlines()[0])
                self.assertEqual(payload["initial_state"], expected)
                self.assertFalse(payload["formal_assets_used"])
                self.assertFalse(payload["valid_import_fixture_uses_formal_data"])
                self.assertFalse(payload["real_provider_calls_allowed"])
                self.assertFalse(payload["market_connections_allowed"])

    def test_valid_import_fixture_exists_only_during_the_runner_lifetime(self) -> None:
        process = subprocess.Popen(
            [
                sys.executable,
                str(RUNNER),
                "--lifetime-seconds",
                "2",
                "--keep-open-after-frozen",
                "--initial-state",
                "bundle-ready",
            ],
            cwd=PROJECT_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        fixture_path: Path | None = None
        try:
            assert process.stdout is not None
            payload = json.loads(process.stdout.readline())
            fixture_path = Path(payload["valid_import_fixture_path"])
            self.assertTrue(fixture_path.is_file())
            self.assertTrue(fixture_path.parent.name.startswith(
                "ai-studio-manual-chatgpt-browser-qa-"
            ))
            fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
            self.assertEqual(fixture["version"], "manual_chatgpt_result_v1")
            self.assertEqual(fixture["room_id"], payload["room_id"])
            _remaining_stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, stderr)
        finally:
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=10)

        self.assertIsNotNone(fixture_path)
        self.assertFalse(fixture_path.exists())

    def test_lifetime_closes_the_ephemeral_listener(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(RUNNER),
                "--lifetime-seconds",
                "1",
                "--keep-open-after-frozen",
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout.splitlines()[0])
        self.assertFalse(payload["formal_assets_used"])
        self.assertFalse(payload["real_provider_calls_allowed"])
        self.assertFalse(payload["market_connections_allowed"])
        self.assertEqual(payload["maximum_lifetime_seconds"], 1)
        self.assertFalse(payload["auto_shutdown_when_frozen"])
        port = int(payload["url"].rsplit(":", 1)[1].rstrip("/"))
        self.assertNotIn(port, {8770, 11111})

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
            client.settimeout(0.5)
            self.assertNotEqual(client.connect_ex(("127.0.0.1", port)), 0)

    def test_frozen_auto_shutdown_tracks_the_latest_room_session(self) -> None:
        source = RUNNER.read_text(encoding="utf-8")

        self.assertIn("current = service.latest(room_id)", source)
        self.assertNotIn('current = service.get(room_id, created["id"])', source)


if __name__ == "__main__":
    unittest.main()
