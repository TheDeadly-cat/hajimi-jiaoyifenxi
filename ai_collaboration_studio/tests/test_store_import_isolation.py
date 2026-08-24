from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
PROVIDER_SECRET_NAMES = (
    "OPENAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "ARK_API_KEY",
    "GLM_API_KEY",
    "ZHIPUAI_API_KEY",
)


class StoreImportIsolationTests(unittest.TestCase):
    def child_environment(self, database_path: Path) -> dict[str, str]:
        environment = os.environ.copy()
        for name in PROVIDER_SECRET_NAMES:
            environment.pop(name, None)
        environment.update(
            {
                "AI_STUDIO_SKIP_LOCAL_ENV": "1",
                "AI_STUDIO_RUNTIME_DIR": str(database_path.parent),
                "AI_STUDIO_DATABASE_PATH": str(database_path),
                "AI_STUDIO_DISABLED_PROVIDERS": "",
            }
        )
        return environment

    def run_child(self, source: str, database_path: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-c", source],
            cwd=PROJECT_DIR,
            env=self.child_environment(database_path),
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )

    def test_importing_http_application_does_not_create_or_migrate_database(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ai-studio-import-only-") as temp_dir:
            database_path = Path(temp_dir) / "must-not-exist.sqlite3"
            result = self.run_child(
                "import backend.http_server\n",
                database_path,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(database_path.exists())

    def test_first_store_access_still_initializes_configured_database(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ai-studio-first-use-") as temp_dir:
            database_path = Path(temp_dir) / "first-use.sqlite3"
            result = self.run_child(
                "from backend.store import STORE\n"
                "assert STORE.path.exists()\n",
                database_path,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(database_path.exists())

    def test_server_startup_opens_current_schema_without_initializing_source(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ai-studio-verified-startup-") as temp_dir:
            database_path = Path(temp_dir) / "verified.sqlite3"
            result = self.run_child(
                "import hashlib,sqlite3\n"
                "from contextlib import closing\n"
                "from backend.config import DATABASE_PATH\n"
                "from backend.store import StudioStore\n"
                "StudioStore(DATABASE_PATH)\n"
                "with closing(sqlite3.connect(DATABASE_PATH)) as connection:\n"
                "    connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')\n"
                "before = hashlib.sha256(DATABASE_PATH.read_bytes()).hexdigest()\n"
                "original_initialize = StudioStore._initialize\n"
                "def guarded_initialize(self):\n"
                "    if self.path == DATABASE_PATH:\n"
                "        raise AssertionError('startup initialized source database')\n"
                "    return original_initialize(self)\n"
                "StudioStore._initialize = guarded_initialize\n"
                "import backend.http_server as http_server\n"
                "http_server.run_server = lambda **kwargs: None\n"
                "import server\n"
                "server.main()\n"
                "after = hashlib.sha256(DATABASE_PATH.read_bytes()).hexdigest()\n"
                "assert before == after\n",
                database_path,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(database_path.exists())

    def test_server_startup_does_not_create_missing_database(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ai-studio-missing-startup-") as temp_dir:
            database_path = Path(temp_dir) / "must-remain-missing.sqlite3"
            result = self.run_child(
                "import server\n"
                "try:\n"
                "    server.main()\n"
                "except SystemExit as exc:\n"
                "    assert exc.code == 1\n"
                "else:\n"
                "    raise AssertionError('missing database startup did not fail')\n",
                database_path,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(database_path.exists())
            self.assertEqual(result.stderr, "")
            events = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["schema_version"], "studio_log_event_v1")
            self.assertEqual(events[0]["event"], "server_start_failed")
            self.assertEqual(
                events[0]["fields"]["phase"],
                "database_preflight_or_host_start",
            )
            self.assertNotIn(str(database_path), result.stdout)

    def test_wrong_owner_is_rejected_before_default_store_initialization(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ai-studio-wrong-owner-") as temp_dir:
            database_path = Path(temp_dir) / "must-not-exist.sqlite3"
            result = self.run_child(
                "from backend.http_server import run_server\n"
                "class RejectingOwner:\n"
                "    def assert_held_for(self, database_path):\n"
                "        raise RuntimeError('wrong owner')\n"
                "try:\n"
                "    run_server(port=0, instance_owner=RejectingOwner())\n"
                "except RuntimeError as exc:\n"
                "    assert str(exc) == 'wrong owner'\n"
                "else:\n"
                "    raise AssertionError('run_server accepted a wrong owner')\n",
                database_path,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(database_path.exists())


if __name__ == "__main__":
    unittest.main()
