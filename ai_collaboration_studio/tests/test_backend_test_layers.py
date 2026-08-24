from __future__ import annotations

import argparse
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch


from scripts import run_backend_tests_isolated as runner


class _RecordingLoader:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def loadTestsFromNames(self, names: list[str]) -> unittest.TestSuite:
        self.calls.append(("modules", list(names)))
        return unittest.TestSuite()

    def discover(
        self,
        *,
        start_dir: str,
        pattern: str,
        top_level_dir: str,
    ) -> unittest.TestSuite:
        self.calls.append(("discover", start_dir, pattern, top_level_dir))
        return unittest.TestSuite()


class BackendTestLayerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.project_root = Path(__file__).resolve().parents[1]
        self.manifest_path = (
            self.project_root / "scripts" / "backend_test_layers.json"
        )
        self.manifest = runner.load_backend_test_layer_manifest(
            self.manifest_path,
            project_root=self.project_root,
        )

    def _load_temporary_manifest(self, value: object) -> dict[str, object]:
        with tempfile.TemporaryDirectory(
            prefix="ai-studio-layer-manifest-test-"
        ) as temp_dir:
            path = Path(temp_dir) / "layers.json"
            path.write_text(
                json.dumps(value, ensure_ascii=False),
                encoding="utf-8",
            )
            return runner.load_backend_test_layer_manifest(
                path,
                project_root=self.project_root,
            )

    def test_manifest_is_closed_versioned_existing_unique_and_canonical(self) -> None:
        self.assertEqual(
            self.manifest["version"], runner.BACKEND_TEST_LAYER_MANIFEST_VERSION
        )
        self.assertEqual(set(self.manifest), {"version", "layers"})
        layers = self.manifest["layers"]
        self.assertEqual(
            [layer["id"] for layer in layers],
            list(runner.BACKEND_TEST_LAYER_IDS),
        )
        assigned: list[str] = []
        for layer in layers:
            self.assertEqual(
                set(layer),
                {
                    "id",
                    "description",
                    "selection",
                    "tests",
                    "start_directory",
                    "pattern",
                },
            )
            if layer["selection"] == "modules":
                self.assertEqual(layer["tests"], sorted(layer["tests"]))
                for module in layer["tests"]:
                    self.assertTrue(
                        self.project_root.joinpath(
                            *module.split(".")
                        ).with_suffix(".py").is_file()
                    )
                assigned.extend(layer["tests"])
        self.assertEqual(len(assigned), len(set(assigned)))
        full = layers[-1]
        self.assertEqual(full["selection"], "discover")
        self.assertEqual(full["tests"], [])
        self.assertEqual(full["start_directory"], "tests")
        self.assertEqual(full["pattern"], "test_*.py")

    def test_manifest_rejects_unknown_fields_versions_duplicates_and_missing_modules(
        self,
    ) -> None:
        invalid_values: list[dict[str, object]] = []

        unknown_root = deepcopy(self.manifest)
        unknown_root["unexpected"] = True
        invalid_values.append(unknown_root)

        unknown_layer = deepcopy(self.manifest)
        unknown_layer["layers"][0]["unexpected"] = True
        invalid_values.append(unknown_layer)

        wrong_version = deepcopy(self.manifest)
        wrong_version["version"] = "backend_test_layers_unsupported"
        invalid_values.append(wrong_version)

        duplicate = deepcopy(self.manifest)
        duplicate["layers"][1]["tests"][0] = duplicate["layers"][0]["tests"][0]
        duplicate["layers"][1]["tests"].sort()
        invalid_values.append(duplicate)

        missing = deepcopy(self.manifest)
        missing["layers"][0]["tests"][0] = "tests.test_missing_layer_module"
        missing["layers"][0]["tests"].sort()
        invalid_values.append(missing)

        for index, value in enumerate(invalid_values):
            with self.subTest(case=index), self.assertRaises(
                runner.BackendTestLayerError
            ):
                self._load_temporary_manifest(value)

    def test_delivery_layer_is_closed_over_delivery_contract_modules(self) -> None:
        delivery = next(
            layer for layer in self.manifest["layers"]
            if layer["id"] == "delivery"
        )
        self.assertEqual(delivery["tests"], [
            "tests.test_ci_delivery_contract",
            "tests.test_delivery_bootstrap",
            "tests.test_dependency_inventory",
            "tests.test_host_delivery_endpoints",
            "tests.test_release_drill",
            "tests.test_source_backup",
            "tests.test_static_security_checks",
            "tests.test_structured_logging",
        ])

    def test_selection_preserves_legacy_modes_and_resolves_layers_without_imports(
        self,
    ) -> None:
        parser = runner._parser()

        explicit = runner.resolve_backend_test_selection(
            parser.parse_args(["tests.test_round_launch_plan"]),
            self.manifest,
        )
        self.assertEqual(explicit, {
            "selection": "modules",
            "tests": ["tests.test_round_launch_plan"],
            "start_directory": "",
            "pattern": "",
        })

        legacy_discover = runner.resolve_backend_test_selection(
            parser.parse_args([]), self.manifest
        )
        self.assertEqual(legacy_discover, {
            "selection": "discover",
            "tests": [],
            "start_directory": "tests",
            "pattern": "test_*.py",
        })

        migration = runner.resolve_backend_test_selection(
            parser.parse_args(["--layer", "migration"]), self.manifest
        )
        self.assertEqual(
            migration["tests"], self.manifest["layers"][0]["tests"]
        )

        full = runner.resolve_backend_test_selection(
            parser.parse_args(["--layer", "full"]), self.manifest
        )
        self.assertEqual(full["selection"], "discover")
        self.assertEqual(full["start_directory"], "tests")

        loader = _RecordingLoader()
        runner.build_backend_test_suite(loader, migration)
        runner.build_backend_test_suite(loader, full)
        self.assertEqual(loader.calls, [
            ("modules", migration["tests"]),
            ("discover", "tests", "test_*.py", "."),
        ])

    def test_layer_selection_rejects_explicit_tests_and_discovery_overrides(self) -> None:
        parser = runner._parser()
        invalid_args: list[argparse.Namespace] = [
            parser.parse_args([
                "--layer",
                "core",
                "tests.test_round_launch_plan",
            ]),
            parser.parse_args([
                "--list-layers",
                "tests.test_round_launch_plan",
            ]),
            parser.parse_args([
                "--layer",
                "domains",
                "--pattern",
                "test_stock*.py",
            ]),
        ]
        for args in invalid_args:
            with self.subTest(args=args), self.assertRaises(
                runner.BackendTestLayerError
            ):
                runner.resolve_backend_test_selection(args, self.manifest)

    def test_duration_reporting_is_bounded_and_rejects_negative_values(self) -> None:
        parser = runner._parser()
        self.assertEqual(parser.parse_args([]).durations, 20)
        self.assertEqual(parser.parse_args(["--durations", "0"]).durations, 0)
        with self.assertRaises(SystemExit):
            runner.main(["--durations", "-1", "tests.test_backend_test_layers"])

    def test_isolated_environment_uses_system_temp_sqlite_and_clears_credentials(
        self,
    ) -> None:
        inherited = {
            "AI_STUDIO_SKIP_LOCAL_ENV": "0",
            "AI_STUDIO_RUNTIME_DIR": str(self.project_root / "runtime"),
            "AI_STUDIO_DATABASE_PATH": str(
                self.project_root / "runtime" / "formal.sqlite3"
            ),
            "FUTU_HOST": "127.0.0.1",
            "FUTU_PORT": "11111",
            "OPENAI_API_KEY": "must-be-cleared",
            "DEEPSEEK_API_KEY": "must-be-cleared",
            "ARK_API_KEY": "must-be-cleared",
            "DOUBAO_API_KEY": "must-be-cleared",
            "GLM_API_KEY": "must-be-cleared",
            "ZHIPU_API_KEY": "must-be-cleared",
            "ZHIPUAI_API_KEY": "must-be-cleared",
            "HTTP_PROXY": "http://127.0.0.1:6553",
            "HTTPS_PROXY": "http://127.0.0.1:6554",
            "ALL_PROXY": "socks5://127.0.0.1:6555",
        }
        with tempfile.TemporaryDirectory(
            prefix="ai-studio-layer-env-test-"
        ) as temp_dir, patch.dict(os.environ, inherited, clear=False):
            runtime = runner.configure_isolated_test_environment(temp_dir)
            runtime.relative_to(Path(tempfile.gettempdir()).resolve())
            self.assertEqual(os.environ["AI_STUDIO_SKIP_LOCAL_ENV"], "1")
            self.assertEqual(os.environ["AI_STUDIO_RUNTIME_DIR"], str(runtime))
            self.assertEqual(
                os.environ["AI_STUDIO_DATABASE_PATH"],
                str(runtime / "unittest-default.sqlite3"),
            )
            self.assertEqual(os.environ["FUTU_HOST"], "127.0.0.1")
            self.assertEqual(os.environ["FUTU_PORT"], "1")
            for name in runner._PROVIDER_KEY_NAMES:
                self.assertNotIn(name, os.environ)
            for name in runner._PROXY_KEY_NAMES:
                self.assertNotIn(name, os.environ)
            self.assertEqual(os.environ["NO_PROXY"], "*")
            self.assertEqual(os.environ["no_proxy"], "*")
            self.assertEqual(os.environ["AI_STUDIO_TEST_NETWORK_GUARD"], "1")
            self.assertEqual(
                Path(os.environ[runner._CHILD_NETWORK_AUDIT_ENV]).resolve(),
                runtime / runner._CHILD_NETWORK_AUDIT_FILE_NAME,
            )
            python_path = os.environ["PYTHONPATH"].split(os.pathsep)
            self.assertEqual(
                Path(python_path[0]).resolve(),
                runner._CHILD_BOOTSTRAP_DIR.resolve(),
            )
            self.assertEqual(
                Path(python_path[1]).resolve(),
                self.project_root.resolve(),
            )
            self.assertEqual(os.environ["PYTHONSAFEPATH"], "1")
            runner.verify_isolated_child_network_bootstrap()
        with self.assertRaises(runner.BackendTestLayerError):
            runner.configure_isolated_test_environment(self.project_root)

    def test_network_guard_allows_ephemeral_loopback_and_blocks_real_targets(
        self,
    ) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        accepted: list[bool] = []

        def accept_once() -> None:
            connection, _address = listener.accept()
            try:
                accepted.append(True)
            finally:
                connection.close()

        thread = threading.Thread(target=accept_once, daemon=True)
        thread.start()
        try:
            with runner.isolated_backend_test_network_guard() as audit:
                with socket.create_connection(listener.getsockname(), timeout=2):
                    pass
                with self.assertRaises(runner.BackendTestNetworkIsolationError):
                    socket.create_connection(("127.0.0.1", 11111), timeout=0.1)
                with self.assertRaises(runner.BackendTestNetworkIsolationError):
                    socket.create_connection(("203.0.113.1", 443), timeout=0.1)
                with self.assertRaises(ConnectionRefusedError):
                    socket.create_connection(("127.0.0.1", 1), timeout=0.1)
                with self.assertRaises(runner.BackendTestNetworkIsolationError):
                    socket.getaddrinfo("provider.example", 443)
            thread.join(timeout=2)
        finally:
            listener.close()

        self.assertFalse(thread.is_alive())
        self.assertEqual(accepted, [True])
        report = audit.report()
        self.assertGreaterEqual(report["allowed_loopback_connections"], 1)
        self.assertEqual(report["simulated_offline_connections"], 1)
        self.assertEqual(report["blocked_attempt_count"], 3)
        self.assertEqual(report["formal_ports_forbidden"], [8770, 11111])
        self.assertTrue(report["non_loopback_forbidden"])

    def test_python_child_process_inherits_the_fatal_network_guard(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="ai-studio-child-network-test-"
        ) as temp_dir, patch.dict(os.environ, {}, clear=False):
            runner.configure_isolated_test_environment(temp_dir)
            test_audit_path = Path(temp_dir) / "expected-child-block.log"
            child_environment = os.environ.copy()
            child_environment[runner._CHILD_NETWORK_AUDIT_ENV] = str(
                test_audit_path
            )
            blocked = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "import socket; "
                        "socket.create_connection(('127.0.0.1',11111),0.1)"
                    ),
                ],
                cwd=self.project_root,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                env=child_environment,
            )
            self.assertEqual(
                blocked.returncode,
                runner._CHILD_NETWORK_BLOCK_EXIT_CODE,
                blocked.stderr,
            )
            audit_rows = test_audit_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(audit_rows), 1)
            self.assertTrue(
                audit_rows[0].endswith("\tcreate_connection:127.0.0.1:11111")
            )
            self.assertEqual(runner.read_child_network_blocks(temp_dir), [])
            self.assertIn(
                "AI_STUDIO_TEST_NETWORK_BLOCKED create_connection:127.0.0.1:11111",
                blocked.stderr,
            )

            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            accepted: list[bool] = []

            def accept_once() -> None:
                connection, _address = listener.accept()
                try:
                    accepted.append(True)
                finally:
                    connection.close()

            thread = threading.Thread(target=accept_once, daemon=True)
            thread.start()
            try:
                allowed = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        (
                            "import socket,sys; "
                            "s=socket.create_connection(('127.0.0.1',int(sys.argv[1])),2); "
                            "s.close()"
                        ),
                        str(listener.getsockname()[1]),
                    ],
                    cwd=self.project_root,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
            finally:
                listener.close()
            thread.join(timeout=2)
            self.assertEqual(allowed.returncode, 0, allowed.stderr)
            self.assertEqual(accepted, [True])
            self.assertFalse(thread.is_alive())

    def test_connected_socket_cannot_become_an_unchecked_send_path(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        received: list[bytes] = []

        def receive_once() -> None:
            connection, _address = listener.accept()
            try:
                received.append(connection.recv(16))
            finally:
                connection.close()

        thread = threading.Thread(target=receive_once, daemon=True)
        thread.start()
        try:
            with runner.isolated_backend_test_network_guard() as audit:
                client = socket.create_connection(listener.getsockname(), timeout=2)
                try:
                    client.sendall(b"allowed")
                finally:
                    client.close()
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as raw:
                    with self.assertRaises(
                        runner.BackendTestNetworkIsolationError
                    ):
                        raw.send(b"blocked")
            thread.join(timeout=2)
        finally:
            listener.close()

        self.assertFalse(thread.is_alive())
        self.assertEqual(received, [b"allowed"])
        report = audit.report()
        self.assertEqual(report["blocked_attempt_count"], 1)
        self.assertEqual(report["blocked_attempts"], ["send:unconnected_inet_socket"])

    def test_child_network_audit_is_closed_to_the_system_temp_runtime(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="ai-studio-child-audit-test-"
        ) as temp_dir:
            runtime = Path(temp_dir)
            self.assertEqual(runner.read_child_network_blocks(runtime), [])
            audit_path = runtime / runner._CHILD_NETWORK_AUDIT_FILE_NAME
            audit_path.write_text(
                "123\tconnect:127.0.0.1:11111\n",
                encoding="utf-8",
            )
            self.assertEqual(
                runner.read_child_network_blocks(runtime),
                ["123\tconnect:127.0.0.1:11111"],
            )

    def test_list_layers_validates_and_prints_without_running_a_suite(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            result = runner.main(["--list-layers"])
        self.assertEqual(result, 0)
        rendered = output.getvalue()
        self.assertIn(
            f"manifest_version={runner.BACKEND_TEST_LAYER_MANIFEST_VERSION}",
            rendered,
        )
        for layer_id in runner.BACKEND_TEST_LAYER_IDS:
            self.assertIn(f"{layer_id}\t", rendered)


if __name__ == "__main__":
    unittest.main()
