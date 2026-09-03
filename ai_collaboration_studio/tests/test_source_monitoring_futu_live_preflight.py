from __future__ import annotations

import ast
import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"

from backend.source_monitoring import futu_live_preflight as preflight_module  # noqa: E402
from backend.source_monitoring.futu_live_preflight import (  # noqa: E402
    FUTU_LIVE_PREFLIGHT_CONFIRMATION,
    FUTU_LIVE_PREFLIGHT_MAX_OUTPUT_BYTES,
    FUTU_LIVE_PREFLIGHT_SDK_VERSION,
    FUTU_LIVE_PREFLIGHT_SYMBOLS,
    FutuLivePreflightConfirmationError,
    FutuLivePreflightDependencies,
    _run_futu_live_preflight_injected,
    validate_futu_live_preflight_report,
)
from backend.source_monitoring.contracts import canonical_sha256  # noqa: E402
from scripts import run_futu_live_preflight as cli_module  # noqa: E402
from scripts.run_futu_live_preflight import (  # noqa: E402
    PREFLIGHT_CONFIRMATION,
    _main_injected as cli_injected_main,
)


FIXED_NOW = datetime(2026, 9, 4, 15, 0, tzinfo=timezone.utc)


class FakeConnection:
    def close(self) -> None:
        return None


class FakeQuoteContext:
    def __init__(
        self,
        *,
        rows: list[dict[str, object]],
        close_error: bool = False,
        state_error: bool = False,
    ) -> None:
        self.rows = rows
        self.close_error = close_error
        self.state_error = state_error
        self.snapshot_symbols: list[str] | None = None
        self.state_symbols: list[str] | None = None

    def get_market_snapshot(self, symbols: list[str]):
        self.snapshot_symbols = list(symbols)
        return 0, copy.deepcopy(self.rows)

    def get_market_state(self, symbols: list[str]):
        self.state_symbols = list(symbols)
        if self.state_error:
            raise RuntimeError("SECRET_STATE_ERROR")
        return 0, [
            {"code": symbol, "market_state": "CLOSED"} for symbol in symbols
        ]

    def close(self) -> None:
        if self.close_error:
            raise RuntimeError("SECRET_CLOSE_ERROR")


class FakeSdk:
    RET_OK = 0

    def __init__(self, context: FakeQuoteContext) -> None:
        self.context = context
        self.open_calls: list[tuple[object, object]] = []

    def OpenQuoteContext(self, *, host: object, port: object) -> FakeQuoteContext:
        self.open_calls.append((host, port))
        return self.context


def quote_rows(*, update_time: str = "2026-09-04 11:00:00") -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for index, symbol in enumerate(FUTU_LIVE_PREFLIGHT_SYMBOLS):
        last = 100.0 + index
        rows.append({
            "code": symbol,
            "name": symbol,
            "update_time": update_time,
            "last_price": last,
            "prev_close_price": last - 1,
            "open_price": last - 0.5,
            "high_price": last + 1,
            "low_price": last - 1,
            "volume": 1000,
            "turnover": 100000,
            "sec_status": "NORMAL",
            "suspension": False,
            "equity_valid": False,
        })
    return rows


def dependencies(
    context: FakeQuoteContext,
    *,
    connection_factory=None,
    sdk_version: str = FUTU_LIVE_PREFLIGHT_SDK_VERSION,
    evidence_class: str = "injected_offline_fixture",
) -> FutuLivePreflightDependencies:
    return FutuLivePreflightDependencies(
        sdk_module=FakeSdk(context),
        sdk_version=sdk_version,
        create_connection=connection_factory or (lambda *_args, **_kwargs: FakeConnection()),
        clock=lambda: FIXED_NOW,
        monotonic=lambda: 100.0,
        snapshot_id_factory=lambda: "fixture_snapshot",
        evidence_class=evidence_class,
    )


class FutuLivePreflightTests(unittest.TestCase):
    def test_exact_confirmation_fails_before_dependencies(self) -> None:
        class PretendConfirmation:
            def __eq__(self, _other: object) -> bool:
                return True

        context = FakeQuoteContext(rows=quote_rows())
        for confirmation in (
            None,
            "",
            "run_futu_live_preflight_once",
            PretendConfirmation(),
        ):
            with self.subTest(confirmation=confirmation):
                with self.assertRaises(FutuLivePreflightConfirmationError):
                    _run_futu_live_preflight_injected(
                        confirmation=confirmation,
                        dependencies=dependencies(context),
                    )
        self.assertEqual(context.snapshot_symbols, None)

    def test_fixture_success_is_fixed_read_only_and_exactly_counted(self) -> None:
        context = FakeQuoteContext(rows=quote_rows())
        report = _run_futu_live_preflight_injected(
            confirmation=FUTU_LIVE_PREFLIGHT_CONFIRMATION,
            dependencies=dependencies(context),
        )

        self.assertTrue(report["ok"])
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["evidence_class"], "injected_offline_fixture")
        self.assertEqual(context.snapshot_symbols, list(FUTU_LIVE_PREFLIGHT_SYMBOLS))
        self.assertIsNone(context.state_symbols)
        self.assertEqual(report["calls"], {
            "quote_batch_attempt_count": 1,
            "quote_batch_return_count": 1,
            "socket_probe_attempt_count": 1,
            "socket_probe_success_count": 1,
            "quote_context_open_attempt_count": 1,
            "quote_context_open_success_count": 1,
            "snapshot_call_attempt_count": 1,
            "snapshot_call_return_count": 1,
            "market_state_call_attempt_count": 0,
            "market_state_call_return_count": 0,
            "close_attempt_count": 1,
            "close_success_count": 1,
        })
        self.assertEqual(report["result"]["ready_symbol_count"], 4)
        self.assertTrue(report["result"]["coverage_complete"])
        safety = report["safety"]
        self.assertEqual(safety["database_reads_performed"], 0)
        self.assertEqual(safety["database_writes_performed"], 0)
        self.assertEqual(safety["provider_calls_performed"], 0)
        self.assertEqual(safety["account_calls_performed"], 0)
        self.assertEqual(safety["order_calls_performed"], 0)
        self.assertEqual(safety["trade_calls_performed"], 0)
        self.assertIsNone(safety["network_requests_performed"])
        self.assertEqual(
            safety["network_requests_accounting"],
            "sdk_transport_not_instrumented",
        )
        self.assertFalse(safety["live_trading_allowed"])
        validate_futu_live_preflight_report(report)
        encoded = json.dumps(report, ensure_ascii=True, sort_keys=True)
        self.assertLess(len(encoded.encode("ascii")), FUTU_LIVE_PREFLIGHT_MAX_OUTPUT_BYTES)

    def test_stale_snapshot_uses_at_most_one_market_state_call(self) -> None:
        context = FakeQuoteContext(
            rows=quote_rows(update_time="2026-09-04 10:30:00")
        )
        report = _run_futu_live_preflight_injected(
            confirmation=FUTU_LIVE_PREFLIGHT_CONFIRMATION,
            dependencies=dependencies(context),
        )
        self.assertTrue(report["ok"])
        self.assertEqual(context.state_symbols, list(FUTU_LIVE_PREFLIGHT_SYMBOLS))
        self.assertEqual(report["calls"]["market_state_call_attempt_count"], 1)
        self.assertEqual(report["calls"]["market_state_call_return_count"], 1)

    def test_offline_opend_stops_before_context_and_redacts_error(self) -> None:
        def offline(*_args, **_kwargs):
            raise OSError("SECRET_LOOPBACK_DETAIL")

        context = FakeQuoteContext(rows=quote_rows())
        report = _run_futu_live_preflight_injected(
            confirmation=FUTU_LIVE_PREFLIGHT_CONFIRMATION,
            dependencies=dependencies(context, connection_factory=offline),
        )
        self.assertFalse(report["ok"])
        self.assertEqual(report["error_code"], "FUTU_OPEND_OFFLINE")
        self.assertEqual(report["calls"]["socket_probe_attempt_count"], 1)
        self.assertEqual(report["calls"]["quote_context_open_attempt_count"], 0)
        self.assertNotIn("SECRET", json.dumps(report, ensure_ascii=True))

    def test_close_failure_is_indeterminate_and_cannot_pass(self) -> None:
        context = FakeQuoteContext(rows=quote_rows(), close_error=True)
        report = _run_futu_live_preflight_injected(
            confirmation=FUTU_LIVE_PREFLIGHT_CONFIRMATION,
            dependencies=dependencies(context),
        )
        self.assertFalse(report["ok"])
        self.assertEqual(report["status"], "indeterminate")
        self.assertEqual(report["error_code"], "FUTU_CONTEXT_CLOSE_UNVERIFIED")
        self.assertEqual(report["calls"]["close_attempt_count"], 1)
        self.assertEqual(report["calls"]["close_success_count"], 0)
        self.assertNotIn("SECRET", json.dumps(report, ensure_ascii=True))

    def test_sdk_missing_or_wrong_version_has_zero_io(self) -> None:
        context = FakeQuoteContext(rows=quote_rows())
        for version, expected in (
            ("", "FUTU_SDK_UNAVAILABLE"),
            ("10.9.0", "FUTU_SDK_VERSION_MISMATCH"),
        ):
            with self.subTest(version=version):
                report = _run_futu_live_preflight_injected(
                    confirmation=FUTU_LIVE_PREFLIGHT_CONFIRMATION,
                    dependencies=dependencies(context, sdk_version=version),
                )
                self.assertEqual(report["error_code"], expected)
                self.assertTrue(all(count == 0 for count in report["calls"].values()))

    def test_guarded_sdk_rejects_noncanonical_host_port_and_symbols(self) -> None:
        context = FakeQuoteContext(rows=quote_rows())
        ledger = preflight_module._CallLedger()
        sdk = preflight_module._GuardedSdk(FakeSdk(context), ledger)
        invalid = (
            ("localhost", 11111),
            ("127.0.0.2", 11111),
            ("::1", 11111),
            ("127.0.0.1 ", 11111),
            ("127.0.0.1", "11111"),
        )
        for host, port in invalid:
            with self.subTest(host=host, port=port), self.assertRaises(
                preflight_module.FutuLivePreflightDependencyError
            ):
                sdk.OpenQuoteContext(host=host, port=port)
        self.assertTrue(all(count == 0 for count in ledger.snapshot().values()))

        guarded = sdk.OpenQuoteContext(host="127.0.0.1", port=11111)
        with self.assertRaises(preflight_module.FutuLivePreflightDependencyError):
            guarded.get_market_snapshot(["US.MU"])
        self.assertEqual(ledger.snapshot()["snapshot_call_attempt_count"], 0)

    def test_receipt_tampering_and_injected_production_claim_are_rejected(self) -> None:
        report = _run_futu_live_preflight_injected(
            confirmation=FUTU_LIVE_PREFLIGHT_CONFIRMATION,
            dependencies=dependencies(FakeQuoteContext(rows=quote_rows())),
        )
        tampered = copy.deepcopy(report)
        tampered["safety"]["trade_calls_performed"] = 1
        tampered.pop("receipt_sha256")
        tampered["receipt_sha256"] = canonical_sha256(tampered)
        with self.assertRaises(preflight_module.FutuLivePreflightError):
            validate_futu_live_preflight_report(tampered)

        false_production = _run_futu_live_preflight_injected(
            confirmation=FUTU_LIVE_PREFLIGHT_CONFIRMATION,
            dependencies=dependencies(
                FakeQuoteContext(rows=quote_rows()),
                evidence_class="production_path_observation",
            ),
        )
        with self.assertRaises(preflight_module.FutuLivePreflightError):
            validate_futu_live_preflight_report(false_production)

        non_ascii_digest = copy.deepcopy(report)
        non_ascii_digest["receipt_sha256"] = "é" * 64
        with self.assertRaises(preflight_module.FutuLivePreflightError):
            validate_futu_live_preflight_report(non_ascii_digest)

        multiline_error = copy.deepcopy(report)
        multiline_error["ok"] = False
        multiline_error["status"] = "failed"
        multiline_error["error_code"] = "FUTU_FIXED_ERROR\nFORGED_SUFFIX"
        multiline_error.pop("receipt_sha256")
        multiline_error["receipt_sha256"] = canonical_sha256(multiline_error)
        with self.assertRaises(preflight_module.FutuLivePreflightError):
            validate_futu_live_preflight_report(multiline_error)

    def test_source_calls_no_forbidden_account_order_or_trade_attributes(self) -> None:
        root = Path(__file__).resolve().parents[1]
        tree = ast.parse(
            (root / "backend" / "source_monitoring" / "futu_live_preflight.py").read_text(
                encoding="utf-8"
            )
        )
        forbidden = {
            "OpenSecTradeContext",
            "OpenFutureTradeContext",
            "unlock_trade",
            "place_order",
            "modify_order",
            "get_acc_list",
            "position_list_query",
            "accinfo_query",
            "subscribe",
            "modify_user_security",
            "set_price_reminder",
        }
        called = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        self.assertEqual(called & forbidden, set())


class FutuLivePreflightCliTests(unittest.TestCase):
    def run_injected(self, argv: list[str], payload: dict[str, object]) -> tuple[int, dict[str, object]]:
        output = io.StringIO()
        code = cli_injected_main(argv, output=output, runner=lambda _confirmation: payload)
        return code, json.loads(output.getvalue())

    def test_help_confirmation_and_unknown_arguments_are_closed(self) -> None:
        output = io.StringIO()
        code = cli_injected_main(["--help"], output=output, runner=lambda _value: {})
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "help")

        for argv, expected in (
            ([], "PREFLIGHT_CONFIRMATION_REQUIRED"),
            (["--confirm", "WRONG"], "PREFLIGHT_CONFIRMATION_REQUIRED"),
            (["--unknown"], "PREFLIGHT_ARGUMENTS_INVALID"),
            (["--confirm", PREFLIGHT_CONFIRMATION, "--confirm", PREFLIGHT_CONFIRMATION], "PREFLIGHT_ARGUMENTS_INVALID"),
        ):
            with self.subTest(argv=argv):
                code, report = self.run_injected(argv, {"ok": True})
                self.assertEqual(code, 2)
                self.assertEqual(report["error_code"], expected)

    def test_environment_sanitizer_removes_credentials_and_futu_overrides(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "SECRET_OPENAI",
                "DEEPSEEK_API_KEY": "SECRET_DEEPSEEK",
                "AI_STUDIO_PROJECT_CAPABILITY_SIGNING_SECRET": "SECRET_SIGNING",
                "SEC_USER_AGENT": "SECRET_CONTACT",
                "FUTU_HOST": "example.com",
                "FUTU_PORT": "9999",
                "GH_TOKEN": "SECRET_GITHUB",
                "AWS_SECRET_ACCESS_KEY": "SECRET_AWS",
            },
            clear=False,
        ):
            environment = cli_module._sanitized_worker_environment(
                Path(tempfile.gettempdir()),
                "a" * 64,
            )
        for name in cli_module._CREDENTIAL_AND_CONFIG_ENV:
            self.assertNotIn(name, environment)
        self.assertNotIn("GH_TOKEN", environment)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", environment)
        self.assertEqual(environment["AI_STUDIO_SKIP_LOCAL_ENV"], "1")
        self.assertEqual(environment[cli_module._WORKER_TOKEN_ENV], "a" * 64)
        self.assertNotIn("SECRET", json.dumps(environment, ensure_ascii=True))

    def test_worker_rebuilds_its_own_environment_defensively(self) -> None:
        project_root = Path(tempfile.gettempdir())
        with mock.patch.dict(
            os.environ,
            {
                "SystemRoot": os.environ.get("SystemRoot", "C:\\Windows"),
                "OPENAI_API_KEY": "SECRET_OPENAI",
                "GH_TOKEN": "SECRET_GITHUB",
                "FUTU_HOST": "example.com",
            },
            clear=True,
        ):
            cli_module._scrub_current_worker_environment(project_root)
            rebuilt = dict(os.environ)
        self.assertNotIn("OPENAI_API_KEY", rebuilt)
        self.assertNotIn("GH_TOKEN", rebuilt)
        self.assertNotIn("FUTU_HOST", rebuilt)
        self.assertEqual(rebuilt["AI_STUDIO_SKIP_LOCAL_ENV"], "1")
        self.assertEqual(rebuilt["AI_STUDIO_RUNTIME_DIR"], str(project_root))

    def test_direct_private_worker_cannot_emit_a_production_receipt(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script = root / "scripts" / "run_futu_live_preflight.py"
        token = "c" * 64
        environment = dict(os.environ)
        environment.update({
            cli_module._WORKER_TOKEN_ENV: token,
            "OPENAI_API_KEY": "SECRET_OPENAI",
            "GH_TOKEN": "SECRET_GITHUB",
        })
        completed = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                str(script),
                cli_module._WORKER_FLAG,
                token,
                "--confirm",
                PREFLIGHT_CONFIRMATION,
            ],
            cwd=root,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
        self.assertIn(completed.returncode, {0, 1})
        self.assertEqual(completed.stderr, b"")
        report = json.loads(completed.stdout.decode("ascii"))
        self.assertEqual(report["evidence_class"], "watchdog_worker_observation")
        self.assertEqual(
            report["receipt_class"],
            "watchdog_worker_observation_not_attestation",
        )
        self.assertFalse(report["safety"]["watchdog_enforced"])
        self.assertFalse(report["safety"]["watchdog_parent_promoted"])
        self.assertNotIn("SECRET", completed.stdout.decode("ascii"))
        validate_futu_live_preflight_report(report)

    def test_watchdog_timeout_is_fixed_indeterminate_error(self) -> None:
        fake_flags = type("Flags", (), {"isolated": 1})()
        with (
            mock.patch.object(cli_module.sys, "flags", fake_flags),
            mock.patch.object(cli_module, "_backend_or_futu_modules_preloaded", return_value=False),
            mock.patch.object(
                cli_module,
                "_run_bounded_worker",
                side_effect=subprocess.TimeoutExpired(["python"], 15),
            ),
        ):
            report = cli_module._run_watchdog_child(PREFLIGHT_CONFIRMATION)
        self.assertEqual(report["error_code"], "PREFLIGHT_WATCHDOG_TIMEOUT")
        self.assertEqual(report["status"], "indeterminate")
        self.assertTrue(report["safety"]["watchdog_enforced"])
        self.assertIsNone(report["safety"]["database_writes_performed"])

    def test_parent_precondition_failure_does_not_claim_watchdog_enforcement(self) -> None:
        fake_flags = type("Flags", (), {"isolated": 0})()
        with mock.patch.object(cli_module.sys, "flags", fake_flags):
            report = cli_module._run_watchdog_child(PREFLIGHT_CONFIRMATION)
        self.assertEqual(report["error_code"], "PREFLIGHT_PARENT_ISOLATION_INVALID")
        self.assertFalse(report["safety"]["watchdog_enforced"])

    def test_worker_start_and_post_run_cleanup_failures_are_distinguished(self) -> None:
        fake_flags = type("Flags", (), {"isolated": 1})()
        with (
            mock.patch.object(cli_module.sys, "flags", fake_flags),
            mock.patch.object(cli_module, "_backend_or_futu_modules_preloaded", return_value=False),
            mock.patch.object(
                cli_module,
                "_worker_command",
                side_effect=OSError("SECRET_RESOLVE"),
            ),
        ):
            command_report = cli_module._run_watchdog_child(PREFLIGHT_CONFIRMATION)
        self.assertEqual(command_report["error_code"], "PREFLIGHT_WORKER_START_FAILED")
        self.assertFalse(command_report["safety"]["watchdog_enforced"])
        self.assertNotIn("SECRET", json.dumps(command_report, ensure_ascii=True))

        with (
            mock.patch.object(cli_module.sys, "flags", fake_flags),
            mock.patch.object(cli_module, "_backend_or_futu_modules_preloaded", return_value=False),
            mock.patch.object(
                cli_module,
                "_run_bounded_worker",
                side_effect=cli_module._WorkerStartFailed("SECRET_START"),
            ),
        ):
            start_report = cli_module._run_watchdog_child(PREFLIGHT_CONFIRMATION)
        self.assertEqual(start_report["error_code"], "PREFLIGHT_WORKER_START_FAILED")
        self.assertFalse(start_report["safety"]["watchdog_enforced"])
        self.assertNotIn("SECRET", json.dumps(start_report, ensure_ascii=True))

        class CleanupFails:
            def __enter__(self) -> str:
                return tempfile.gettempdir()

            def __exit__(self, *_args: object) -> None:
                raise OSError("SECRET_CLEANUP")

        with (
            mock.patch.object(cli_module.sys, "flags", fake_flags),
            mock.patch.object(cli_module, "_backend_or_futu_modules_preloaded", return_value=False),
            mock.patch.object(cli_module.tempfile, "TemporaryDirectory", return_value=CleanupFails()),
            mock.patch.object(
                cli_module,
                "_run_bounded_worker",
                return_value=subprocess.CompletedProcess([], 1, stdout=b"{}", stderr=b""),
            ),
        ):
            cleanup_report = cli_module._run_watchdog_child(PREFLIGHT_CONFIRMATION)
        self.assertEqual(
            cleanup_report["error_code"],
            "PREFLIGHT_WORKER_LIFECYCLE_FAILED",
        )
        self.assertTrue(cleanup_report["safety"]["watchdog_enforced"])
        self.assertNotIn("SECRET", json.dumps(cleanup_report, ensure_ascii=True))

    def test_worker_stdout_is_bounded_before_parent_accumulation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="futu-output-bound-test-") as temp_dir:
            with self.assertRaises(subprocess.SubprocessError):
                cli_module._run_bounded_worker(
                    [
                        sys.executable,
                        "-I",
                        "-B",
                        "-c",
                        "import sys; sys.stdout.buffer.write(b'x' * 20000)",
                    ],
                    cwd=temp_dir,
                    environment=cli_module._sanitized_worker_environment(
                        Path(temp_dir),
                        "b" * 64,
                    ),
                )

    def test_real_isolated_cli_is_bounded_redacted_and_never_uses_env_target(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script = root / "scripts" / "run_futu_live_preflight.py"
        sentinel = root / ".futu-live-preflight-database-must-not-open.sqlite3"
        self.assertFalse(sentinel.exists())
        environment = dict(os.environ)
        environment.update({
            "OPENAI_API_KEY": "SECRET_OPENAI",
            "DEEPSEEK_API_KEY": "SECRET_DEEPSEEK",
            "FUTU_HOST": "example.com",
            "FUTU_PORT": "9999",
        })
        completed = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                str(script),
                "--confirm",
                PREFLIGHT_CONFIRMATION,
            ],
            cwd=root,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
        self.assertIn(completed.returncode, {0, 1})
        self.assertEqual(completed.stderr, b"")
        self.assertLess(len(completed.stdout), cli_module.MAX_OUTPUT_BYTES)
        report = json.loads(completed.stdout.decode("ascii"))
        self.assertEqual(report["evidence_class"], "production_path_observation")
        self.assertTrue(report["safety"]["isolated_cli_import_guard_attested"])
        self.assertTrue(report["safety"]["watchdog_guard_attested"])
        self.assertTrue(report["safety"]["watchdog_enforced"])
        self.assertTrue(report["safety"]["watchdog_parent_promoted"])
        self.assertFalse(report["safety"]["live_trading_allowed"])
        self.assertTrue(all(count <= 1 for count in report["calls"].values()))
        self.assertNotIn("SECRET", completed.stdout.decode("ascii"))
        self.assertFalse(sentinel.exists())
        validate_futu_live_preflight_report(report)


if __name__ == "__main__":
    unittest.main()
