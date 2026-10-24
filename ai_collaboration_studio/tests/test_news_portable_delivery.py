from __future__ import annotations

import hashlib
from contextlib import closing
import io
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from scripts import news_portable_runtime as portable
from scripts import verify_news_clean_checkout as verify


class NewsPortableDeliveryTests(unittest.TestCase):
    def test_environment_scrubs_all_supported_credentials_and_inherited_data(self):
        original = {name: "test-only-placeholder" for name in portable.PROVIDER_ENVIRONMENT_NAMES}
        original.update({"AI_STUDIO_DATABASE_PATH": "unrelated-database", "AI_STUDIO_PORT": "8770",
                         "HTTP_PROXY": "http://invalid.example", "PYTHONPATH": "untrusted", "KEEP": "yes"})
        clean = portable.safe_environment(original)
        self.assertTrue(all(name not in clean for name in portable.PROVIDER_ENVIRONMENT_NAMES))
        self.assertEqual(clean["AI_STUDIO_SKIP_LOCAL_ENV"], "1")
        self.assertEqual(clean["AI_STUDIO_DISABLED_PROVIDERS"], portable.DISABLED_PROVIDERS)
        self.assertNotIn("AI_STUDIO_DATABASE_PATH", clean)
        self.assertNotIn("AI_STUDIO_PORT", clean)
        self.assertNotIn("HTTP_PROXY", clean)
        self.assertNotIn("PYTHONPATH", clean)
        self.assertEqual(clean["KEEP"], "yes")
        self.assertEqual(original["AI_STUDIO_PORT"], "8770")

    def test_default_data_root_is_project_local_and_production_ports_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "checkout"
            self.assertEqual(portable.data_root(project), project / "runtime/news-workspace")
            self.assertFalse(project.exists())
        for port in (8770, 11111, 18787, 80, 0, 65536, True):
            with self.assertRaises(portable.PortableError):
                portable.validate_port(port)
        self.assertEqual(portable.validate_port(8780), 8780)

    def test_occupied_port_refuses_without_reusing_or_stopping_listener(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            with self.assertRaisesRegex(portable.PortableError, "port_unavailable"):
                portable.reserve_port(port)
            self.assertEqual(listener.getsockname()[1], port)
            self.assertNotEqual(listener.fileno(), -1)

    def test_new_initialization_refuses_empty_database_and_other_existing_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(portable.require_empty_data(root), root)
            db = root / portable.DATABASE_NAMES[0]
            db.touch()
            with self.assertRaisesRegex(portable.PortableError, "requires_empty"):
                portable.require_empty_data(root)
            self.assertEqual(db.read_bytes(), b"")
            with self.assertRaisesRegex(portable.PortableError, "missing_or_empty"):
                portable.closed_database(db)

    def test_existing_old_news_schema_is_refused_without_changing_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "news.sqlite3"
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("PRAGMA application_id=1313167187")
                db.execute("CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
                db.execute("INSERT INTO settings VALUES('enabled','false')")
                db.execute("CREATE TABLE sources(id TEXT PRIMARY KEY)")
            before = path.read_bytes()
            with self.assertRaisesRegex(portable.PortableError, "explicit_migration"):
                portable.preflight_news_schema(path)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(sorted(p.name for p in path.parent.iterdir()), ["news.sqlite3"])

    def test_existing_sidecars_require_explicit_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "news.sqlite3"
            path.write_bytes(b"synthetic fixture")
            sidecar = Path(str(path) + "-wal")
            sidecar.write_bytes(b"retained")
            with self.assertRaisesRegex(portable.PortableError, "explicit_recovery"):
                portable.closed_database(path)
            self.assertEqual(sidecar.read_bytes(), b"retained")

    def test_analysis_preflight_is_read_only_and_does_not_recover_pending_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "analysis.sqlite3"
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("PRAGMA application_id=1312901425")
                db.execute("CREATE TABLE runs(fingerprint TEXT PRIMARY KEY,item_id TEXT,created REAL,reserved INTEGER,status TEXT,result TEXT)")
                db.execute("INSERT INTO runs VALUES('fixture','fixture',0,0,'pending','{}')")
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertTrue(portable.preflight_news_schema(path, analysis=True)["readonly"])
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)

    def test_offline_mode_blocks_loopback_external_dns_datagrams_and_subprocess(self):
        with portable.offline_guard():
            with socket.socket() as client:
                for address in (("127.0.0.1", 8780), ("192.0.2.1", 443)):
                    with self.assertRaisesRegex(portable.PortableError, "outbound_blocked"):
                        client.connect(address)
                    with self.assertRaises(portable.PortableError):
                        client.connect_ex(address)
                with self.assertRaises(portable.PortableError):
                    client.sendto(b"fixture", ("192.0.2.1", 53))
            with self.assertRaises(portable.PortableError):
                socket.getaddrinfo("example.invalid", 443)
            with self.assertRaises(portable.PortableError):
                subprocess.Popen(["not-executed"])
            self.assertEqual(socket.gethostbyaddr("127.0.0.1")[0], "localhost")

    def test_toolchain_is_exact_and_mismatch_stops_before_install(self):
        calls = []
        def runner(arguments, **kwargs):
            calls.append(arguments)
            name = arguments[0]
            return subprocess.CompletedProcess(arguments, 0, portable.EXPECTED_TOOLCHAIN[name] + "\n", "")
        result = portable.verify_toolchain("node", "npm", environment={}, cwd=Path.cwd(), runner=runner)
        self.assertTrue(result["matches"])
        self.assertEqual(calls, [["node", "--version"], ["npm", "--version"]])
        def mismatch(arguments, **kwargs):
            return subprocess.CompletedProcess(arguments, 0, "v0.0.0\n", "not persisted")
        with self.assertRaisesRegex(portable.PortableError, "version_mismatch"):
            portable.verify_toolchain("node", "npm", environment={}, cwd=Path.cwd(), runner=mismatch)

    def test_payload_gate_rejects_enabled_collection_paid_configuration_and_wrong_build(self):
        readiness = {"ready": True, "ok": True}
        version = {"backend_build": {"source_sha256": "fixture"}}
        news = {"enabled": False, "running": True, "counts": {"total": 0}, "recent_checks": [],
                "ai": {"configured": False, "busy": False, "calls_reserved": 0}}
        self.assertFalse(verify.verify_payloads(readiness, version, {"overview": news}, "fixture")["source_recovery_verified"])
        for changed in ({**news, "enabled": True}, {**news, "ai": {**news["ai"], "configured": True}},
                        {**news, "ai": {**news["ai"], "calls_reserved": True}}):
            with self.assertRaises(portable.PortableError):
                verify.verify_payloads(readiness, version, {"overview": changed}, "fixture")
        with self.assertRaisesRegex(portable.PortableError, "identity_mismatch"):
            verify.verify_payloads(readiness, version, {"overview": news}, "different")

    def test_child_shutdown_uses_only_created_handle_and_never_pid_search(self):
        class Child:
            def __init__(self):
                self.stdin = io.StringIO()
                self.returncode = None
                self.terminated = False
                self.request = None
            def poll(self):
                return self.returncode
            def wait(self, timeout):
                self.request = self.stdin.getvalue()
                self.returncode = 0
                return 0
            def terminate(self):
                self.terminated = True
        child = Child()
        result = verify.stop_owned_child(child)
        self.assertEqual(child.request, "stop\n")
        self.assertFalse(child.terminated)
        self.assertEqual(result, {"exit_code": 0, "forced": False, "scope": "owned_popen_handle_only"})

    def test_real_new_database_initialization_and_preflight_are_isolated_and_offline(self):
        project = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="news-portable-new-data-test-") as directory:
            root = Path(directory) / "new-data"
            root.mkdir()
            environment = portable.safe_environment()
            environment.update({"AI_STUDIO_RUNTIME_DIR": str(root),
                                "AI_STUDIO_DATABASE_PATH": str(root / portable.DATABASE_NAMES[0])})
            code = """import json,sys,traceback
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from scripts.news_portable_runtime import offline_guard
try:
    with offline_guard():
        from scripts.run_news_workspace import initialize_new_databases,preflight
        root=Path(sys.argv[2])
        initialize_new_databases(root)
        _,summary=preflight(root)
        print(json.dumps(summary))
except Exception as exc:
    print(json.dumps({'error_type':type(exc).__name__,'frames':[
        {'file':Path(f.filename).name,'line':f.lineno,'function':f.name}
        for f in traceback.extract_tb(exc.__traceback__)]}))
    sys.exit(1)
"""
            completed = subprocess.run([sys.executable, "-I", "-B", "-X", "utf8", "-c", code,
                                        str(project), str(root)], env=environment, cwd=project,
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                       encoding="utf-8", timeout=60, check=False,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.assertEqual(completed.returncode, 0, completed.stdout)
            summary = json.loads(completed.stdout)
            self.assertTrue(summary["studio_schema_ready"])
            self.assertFalse(summary["implicit_migration"])
            self.assertFalse(summary["implicit_recovery"])
            self.assertEqual(sorted(p.name for p in root.iterdir()), sorted(portable.DATABASE_NAMES))


if __name__ == "__main__":
    unittest.main()
