"""Welcome coordination with synthetic paths and identities; no host/download."""
from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from scripts import news_first_run as setup
from scripts import run_news_workspace as entry


class WelcomeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="news-welcome-offline-")
        self.root = Path(self.temporary.name) / "中文 space"
        self.project = self.root / "project"
        (self.project / "frontend/dist").mkdir(parents=True)
        (self.project / "backend").mkdir()
        (self.project / "server.py").write_text("# synthetic only\n", encoding="utf-8")
        (self.project / "frontend/dist/index.html").write_text("synthetic", encoding="utf-8")
        self.data = self.root / "data"
        self.python = self.root / "not-executed.exe"
        self.port = 19341

    def tearDown(self):
        self.temporary.cleanup()

    def inspect(self, **kwargs):
        return setup.inspect_workspace(self.project, self.data, self.port, self.python,
                                       own=lambda *_: None, dependencies=lambda *_: True, **kwargs)

    def record(self):
        value = {"project": str(self.project), "data_root": str(self.data), "port": self.port,
                 "pid": 4321, "creation_filetime": "134000000000000123",
                 "backend_sha256": setup.portable.backend_identity(self.project),
                 "frontend_sha256": setup.sha(self.project / "frontend/dist/index.html")}
        setup.write_binding(setup.binding_path(self.project, self.data, self.port), value)
        return value

    def test_first_open_is_explicit_and_does_not_create_data(self):
        value = self.inspect()
        self.assertEqual(value["code"], "first_run")
        self.assertEqual(value["action"], "initialize")
        self.assertFalse(self.data.exists())
        with patch.object(setup, "inspect_workspace", return_value=value), patch.object(setup.subprocess, "Popen") as start:
            self.assertEqual(setup.launch(self.project, self.data, self.port, self.python)["code"], "first_run")
            start.assert_not_called()

    def test_partial_and_existing_zero_byte_files_are_never_new_data(self):
        self.data.mkdir()
        (self.data / "collaboration_studio.sqlite3").write_bytes(b"")
        self.assertEqual(self.inspect()["code"], "partial_data")
        with patch.object(setup, "inspect_workspace", return_value=self.inspect()), patch.object(setup.subprocess, "Popen") as start:
            setup.launch(self.project, self.data, self.port, self.python, initialize=True)
            start.assert_not_called()
        self.assertEqual((self.data / "collaboration_studio.sqlite3").read_bytes(), b"")

    def test_sidecars_and_pending_recovery_remain_untouched(self):
        self.data.mkdir()
        for suffix in ("-wal", "-shm", "-journal", ".startup-recovery.pending.json"):
            path = self.data / ("collaboration_studio.sqlite3" + suffix)
            path.write_bytes(b"synthetic keep")
            self.assertEqual(self.inspect()["code"], "recovery_required")
            self.assertEqual(path.read_bytes(), b"synthetic keep")
            path.unlink()

    def test_existing_data_requires_launch_not_initialization(self):
        self.data.mkdir()
        for name in setup.portable.DATABASE_NAMES:
            (self.data / name).write_bytes(b"synthetic contents; no database open")
        value = self.inspect()
        self.assertEqual(value["code"], "ready_to_start")
        with patch.object(setup, "inspect_workspace", return_value=value), patch.object(setup.subprocess, "Popen") as start:
            setup.launch(self.project, self.data, self.port, self.python, initialize=True)
            start.assert_not_called()

    def test_unbound_occupied_port_is_never_opened_or_probed(self):
        with patch.object(setup, "get_local", side_effect=AssertionError("not our server")):
            code = setup.own_instance(self.project, self.data, self.port,
                                     listener=lambda _: 9999, get=lambda *_: self.fail("HTTP must not be requested"))
        self.assertEqual(code, "occupied_port")

    def test_same_pid_different_creation_time_is_not_reused(self):
        self.record()
        code = setup.own_instance(self.project, self.data, self.port, listener=lambda _: 4321,
                                 identity=lambda _: {"pid": 4321, "creation_filetime": "134000000000000124", "exited": False},
                                 get=lambda *_: self.fail("PID alone must not authorize GET"))
        self.assertEqual(code, "identity_unknown")

    def test_known_instance_requires_matching_listener_data_and_both_builds(self):
        record = self.record()
        def get(_port, endpoint):
            if endpoint == "/api/readiness":
                return {"ready": True, "service": {"id": "ai_collaboration_studio"}}
            return {"service": {"id": "ai_collaboration_studio"},
                    "backend_build": {"source_sha256": record["backend_sha256"]},
                    "frontend_build": {"index_sha256": record["frontend_sha256"]}}
        kwargs = {"listener": lambda _: 4321, "identity": lambda _: {
            "pid": 4321, "creation_filetime": record["creation_filetime"], "exited": False}, "get": get}
        self.assertEqual(setup.own_instance(self.project, self.data, self.port, **kwargs), "already_running")
        (self.project / "frontend/dist/index.html").write_text("different build", encoding="utf-8")
        self.assertEqual(setup.own_instance(self.project, self.data, self.port, **kwargs), "identity_unknown")
        self.assertIsNone(setup.own_instance(self.project, self.data / "other", self.port, listener=lambda _: None))

    def test_live_owned_instance_is_checked_before_any_database_inspection(self):
        value = setup.inspect_workspace(self.project, self.data, self.port, self.python,
            own=lambda *_: "already_running", dependencies=lambda *_: self.fail("do not reinitialize live data"))
        self.assertEqual(value["code"], "already_running")
        self.assertFalse(self.data.exists())

    def test_dead_binding_allows_recheck_but_never_reuses_replacement_listener(self):
        self.record()
        self.assertIsNone(setup.own_instance(self.project, self.data, self.port,
                                           listener=lambda _: None, identity=lambda _: None))
        self.assertEqual(setup.own_instance(self.project, self.data, self.port,
            listener=lambda _: 7777, identity=lambda _: None), "occupied_port")

    def test_default_entry_failure_always_gets_a_new_safe_receipt(self):
        with patch.object(entry, "PROJECT", self.project), patch.object(entry.portable, "data_root", side_effect=entry.portable.PortableError("synthetic_refusal")), patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(entry.main([]), 1)
        receipts = list((self.project / "runtime/news-launcher/receipts").glob("*-startup.json"))
        self.assertEqual(len(receipts), 1)
        value = json.loads(receipts[0].read_text(encoding="utf-8"))
        self.assertEqual(value["error_code"], "synthetic_refusal")
        self.assertFalse(value["ok"])

    def test_explicit_new_receipt_survives_early_sidecar_refusal(self):
        self.data.mkdir()
        (self.data / "collaboration_studio.sqlite3").write_bytes(b"synthetic preserve")
        sidecar = self.data / "collaboration_studio.sqlite3-wal"
        sidecar.write_bytes(b"synthetic WAL preserve")
        target = self.root / "explicit-receipts/refused.json"
        with patch.object(entry, "PROJECT", self.project), patch("sys.stderr", new_callable=io.StringIO):
            code = entry.main(["--data-root", str(self.data), "--port", str(self.port), "--receipt", str(target)])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["error_code"], "database_sidecars_require_explicit_recovery")
        self.assertEqual(sidecar.read_bytes(), b"synthetic WAL preserve")
        self.assertFalse((self.project / "runtime/news-launcher/receipts").exists())

    def test_named_gate_prevents_a_second_launch_owner(self):
        outcomes = []
        def concurrent_attempt():
            try:
                with setup.launch_gate(self.project, self.data, self.port, timeout_ms=0):
                    outcomes.append("unexpected_owner")
            except setup.SetupError as exc:
                outcomes.append(str(exc))
        with setup.launch_gate(self.project, self.data, self.port):
            thread = threading.Thread(target=concurrent_attempt)
            thread.start(); thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(outcomes, ["startup_pending"])
        with setup.launch_gate(self.project, self.data, self.port, timeout_ms=0):
            pass

    def test_safe_copy_excludes_paths_body_and_secret_values(self):
        value = setup.status("startup_failed", path=str(self.data), key="SYNTHETIC_NOT_REAL", body="synthetic body")
        text = setup.safe_diagnostic(value)
        for forbidden in (str(self.data), "SYNTHETIC_NOT_REAL", "synthetic body"):
            self.assertNotIn(forbidden, text)

    def test_runtime_manifest_uses_full_python_and_fixed_official_hashes(self):
        manifest = json.loads((setup.PROJECT / "scripts/news_first_run_runtime.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["python"]["url"], "https://www.python.org/ftp/python/3.14.6/python-3.14.6-amd64.zip")
        self.assertEqual(manifest["python"]["sha256"], "75afa83f93b284d19040e24bc440ab741c09582c0d5310504d607a4e08c3dbaf")
        self.assertEqual(manifest["node"]["sha256"], "edaca9bd58ec8e92037dac4e877d52f6b8f430b81c18b57e264b4e2fb111cd56")
        self.assertEqual(manifest["node"]["npm_version"], setup.portable.EXPECTED_TOOLCHAIN["npm"])
        self.assertEqual((setup.PROJECT / "scripts/start_news_workspace.ps1").read_bytes()[:3], b"\xef\xbb\xbf")


if __name__ == "__main__":
    unittest.main()
