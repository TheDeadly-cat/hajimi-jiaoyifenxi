from __future__ import annotations

import ast
import contextlib
import io
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.source_poll_control import SourcePollCancelled, SourcePollDeadlineExceeded
from backend.source_monitoring import futu_readonly_broker as broker_module
from backend.source_monitoring.futu_readonly_broker import (
    FUTU_READONLY_BROKER_POLICY_SHA256,
    FUTU_READONLY_BROKER_SYMBOLS,
    FutuReadOnlyBroker,
    FutuReadOnlyBrokerError,
)


def _worker_program(
    *,
    managed: bool,
    prefix: str = "",
    stderr_prefix: str = "",
    suffix: str = "",
    delay: float = 0.0,
) -> str:
    calls = {name: 0 for name in broker_module._CALL_KEYS}
    calls.update({
        "quote_batch_attempt_count": 1,
        "quote_batch_return_count": 1,
        "socket_probe_attempt_count": 1,
    })
    return (
        "import hashlib,json,sys,time\n"
        f"symbols={list(FUTU_READONLY_BROKER_SYMBOLS)!r}\n"
        f"calls={calls!r}\n"
        f"policy={FUTU_READONLY_BROKER_POLICY_SHA256!r}\n"
        f"prefix={prefix!r}\n"
        f"stderr_prefix={stderr_prefix!r}\n"
        f"suffix={suffix!r}\n"
        f"delay={delay!r}\n"
        f"managed={managed!r}\n"
        "for line in sys.stdin.buffer:\n"
        " req=json.loads(line.decode('ascii'))\n"
        " if delay: time.sleep(delay)\n"
        " if prefix: sys.stdout.buffer.write((prefix+'\\n').encode('ascii')); sys.stdout.buffer.flush()\n"
        " if stderr_prefix: sys.stderr.write(stderr_prefix+'\\n'); sys.stderr.flush()\n"
        " snapshot={'symbols':symbols,'execution_capability':'none','live_trading_allowed':False}\n"
        " out={'version':'futu_readonly_broker_response_v1','request_id':req['request_id'],'ok':True,'error_code':'','sdk_version':'10.10.7008','snapshot':snapshot,'calls':calls,'policy_sha256':policy,'execution_capability':'none','live_trading_allowed':False}\n"
        " raw=json.dumps(out,ensure_ascii=True,sort_keys=True,separators=(',',':'),allow_nan=False).encode('ascii')\n"
        " out['response_sha256']=hashlib.sha256(raw).hexdigest()\n"
        " sys.stdout.buffer.write((json.dumps(out,ensure_ascii=True,sort_keys=True,separators=(',',':'),allow_nan=False)+'\\n').encode('ascii')); sys.stdout.buffer.flush()\n"
        " if suffix: sys.stdout.buffer.write((suffix+'\\n').encode('ascii')); sys.stdout.buffer.flush()\n"
        " if not managed: break\n"
    )


def _command_for(program: str):
    return lambda _mode: [sys.executable, "-I", "-B", "-c", program]


class FutuReadOnlyBrokerTests(unittest.TestCase):
    def test_sdk_import_is_lazy_and_confined_to_worker_loader(self) -> None:
        path = (
            Path(__file__).resolve().parents[1]
            / "backend"
            / "source_monitoring"
            / "futu_readonly_broker.py"
        )
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        parents: dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent
        imports: list[str] = []
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "importlib"
                and node.func.attr == "import_module"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "futu"
            ):
                continue
            current: ast.AST | None = node
            while current is not None and not isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
                current = parents.get(current)
            imports.append(current.name if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)) else "")
        self.assertEqual(imports, ["_load_worker_sdk"])
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

    def test_construction_and_invalid_request_are_zero_process(self) -> None:
        loaded_before = {name for name in sys.modules if name == "futu" or name.startswith("futu.")}
        broker = FutuReadOnlyBroker(mode="managed")
        self.assertIsNone(broker._process)
        self.assertIsNone(broker._temporary_directory)
        self.assertEqual(
            {name for name in sys.modules if name == "futu" or name.startswith("futu.")},
            loaded_before,
        )
        with self.assertRaisesRegex(FutuReadOnlyBrokerError, "read-only Futu broker"):
            broker.quote_batch(["US.MU"], force=True)
        self.assertIsNone(broker._process)

    def test_worker_environment_is_allowlisted_and_token_is_not_in_command(self) -> None:
        with tempfile.TemporaryDirectory(prefix="futu-broker-env-test-") as root:
            profile = Path(root) / broker_module._SDK_PROFILE_DIRNAME
            profile.mkdir()
            with patch.dict(
                os.environ,
                {
                    "OPENAI_API_KEY": "SECRET_OPENAI",
                    "SEC_USER_AGENT": "SECRET_CONTACT",
                    "FUTU_HOST": "remote.invalid",
                    "FUTU_PORT": "9999",
                    "GH_TOKEN": "SECRET_GITHUB",
                },
                clear=False,
            ):
                environment = broker_module._worker_environment(profile, "a" * 64)
            with patch.dict(
                os.environ,
                {
                    "OPENAI_API_KEY": "SECRET_OPENAI",
                    "GH_TOKEN": "SECRET_GITHUB",
                    "APPDATA": str(profile.resolve()),
                    "LOCALAPPDATA": str(profile.resolve()),
                },
                clear=True,
            ):
                broker_module._scrub_worker_environment(profile.resolve())
                scrubbed = dict(os.environ)
        self.assertEqual(environment["APPDATA"], str(profile.resolve()))
        self.assertEqual(environment["LOCALAPPDATA"], str(profile.resolve()))
        self.assertNotIn("OPENAI_API_KEY", environment)
        self.assertNotIn("SEC_USER_AGENT", environment)
        self.assertNotIn("FUTU_HOST", environment)
        self.assertNotIn("FUTU_PORT", environment)
        self.assertNotIn("GH_TOKEN", environment)
        self.assertNotIn("OPENAI_API_KEY", scrubbed)
        self.assertNotIn("GH_TOKEN", scrubbed)
        self.assertEqual(scrubbed["APPDATA"], str(profile.resolve()))
        self.assertEqual(scrubbed["LOCALAPPDATA"], str(profile.resolve()))
        self.assertNotIn("a" * 64, broker_module._worker_command("managed"))

    def test_post_io_worker_failure_preserves_bounded_call_ledger(self) -> None:
        class FakeConnection:
            def close(self) -> None:
                return None

        class ExplodingSdk:
            RET_OK = 0

            @staticmethod
            def OpenQuoteContext(*, host, port):
                del host, port
                raise KeyboardInterrupt("SECRET_WORKER_FAILURE")

        request_id = "a" * 32
        with patch("socket.create_connection", return_value=FakeConnection()):
            response = broker_module._worker_quote_response(
                request_id,
                sdk=ExplodingSdk(),
                sdk_version="10.10.7008",
            )

        self.assertFalse(response["ok"])
        self.assertEqual(response["error_code"], "FUTU_BROKER_WORKER_INTERNAL_ERROR")
        self.assertEqual(response["calls"]["quote_batch_attempt_count"], 1)
        self.assertEqual(response["calls"]["quote_batch_return_count"], 0)
        self.assertEqual(response["calls"]["socket_probe_attempt_count"], 1)
        self.assertEqual(response["calls"]["socket_probe_success_count"], 1)
        self.assertEqual(response["calls"]["quote_context_open_attempt_count"], 1)
        self.assertEqual(response["calls"]["quote_context_open_success_count"], 0)
        self.assertNotIn("SECRET", str(response))
        broker_module._validate_worker_response(response, request_id)

    def test_one_shot_and_managed_use_same_bounded_protocol(self) -> None:
        for mode in ("one_shot", "managed"):
            with self.subTest(mode=mode), patch.object(
                broker_module,
                "_worker_command",
                _command_for(_worker_program(managed=mode == "managed")),
            ):
                loaded_before = {
                    name
                    for name in sys.modules
                    if name == "futu" or name.startswith("futu.")
                }
                broker = FutuReadOnlyBroker(mode=mode)
                snapshot = broker.quote_batch(FUTU_READONLY_BROKER_SYMBOLS, force=True)
                self.assertEqual(snapshot["symbols"], list(FUTU_READONLY_BROKER_SYMBOLS))
                self.assertEqual(snapshot["execution_capability"], "none")
                self.assertFalse(snapshot["live_trading_allowed"])
                self.assertEqual(
                    {
                        name
                        for name in sys.modules
                        if name == "futu" or name.startswith("futu.")
                    },
                    loaded_before,
                )
                if mode == "one_shot":
                    self.assertIsNone(broker._process)
                    with self.assertRaises(FutuReadOnlyBrokerError) as raised:
                        broker.quote_batch(FUTU_READONLY_BROKER_SYMBOLS, force=True)
                    self.assertEqual(raised.exception.code, "FUTU_BROKER_ONE_SHOT_CONSUMED")
                else:
                    first_process = broker._process
                    profile_root = Path(broker._temporary_directory.name)
                    broker.quote_batch(FUTU_READONLY_BROKER_SYMBOLS, force=True)
                    self.assertIs(broker._process, first_process)
                    self.assertTrue(broker.stop())
                    self.assertIsNone(broker._process)
                    self.assertFalse(profile_root.exists())

    def test_cancel_and_deadline_kill_inflight_worker(self) -> None:
        program = _worker_program(managed=True, delay=60.0)
        with patch.object(broker_module, "_worker_command", _command_for(program)):
            broker = FutuReadOnlyBroker(mode="managed", timeout_ms=60_000)
            cancel = threading.Event()
            timer = threading.Timer(0.1, cancel.set)
            timer.start()
            started = time.monotonic()
            try:
                with self.assertRaises(SourcePollCancelled):
                    broker.quote_batch(
                        FUTU_READONLY_BROKER_SYMBOLS,
                        force=True,
                        cancel_event=cancel,
                    )
            finally:
                timer.cancel()
            self.assertLess(time.monotonic() - started, 3)
            self.assertIsNone(broker._process)

        with patch.object(broker_module, "_worker_command", _command_for(program)):
            broker = FutuReadOnlyBroker(mode="managed", timeout_ms=60_000)
            deadline = int(time.monotonic() * 1_000) + 100
            with self.assertRaises(SourcePollDeadlineExceeded):
                broker.quote_batch(
                    FUTU_READONLY_BROKER_SYMBOLS,
                    force=True,
                    deadline_monotonic_ms=deadline,
                )
            self.assertIsNone(broker._process)

    def test_worker_noise_never_reaches_studio_stdout_and_fails_closed(self) -> None:
        program = _worker_program(managed=True, prefix="UNTRUSTED_SDK_STDOUT")
        captured = io.StringIO()
        with (
            patch.object(broker_module, "_worker_command", _command_for(program)),
            contextlib.redirect_stdout(captured),
        ):
            broker = FutuReadOnlyBroker(mode="managed")
            with self.assertRaises(FutuReadOnlyBrokerError) as raised:
                broker.quote_batch(FUTU_READONLY_BROKER_SYMBOLS, force=True)
        self.assertIn(raised.exception.code, {"FUTU_BROKER_PROTOCOL_INVALID", "FUTU_BROKER_OUTPUT_INVALID"})
        self.assertEqual(captured.getvalue(), "")
        self.assertIsNone(broker._process)

        suffix_program = _worker_program(
            managed=False,
            suffix="UNTRUSTED_TRAILING_STDOUT",
        )
        with patch.object(
            broker_module,
            "_worker_command",
            _command_for(suffix_program),
        ):
            broker = FutuReadOnlyBroker(mode="one_shot")
            with self.assertRaises(FutuReadOnlyBrokerError) as trailing:
                broker.quote_batch(FUTU_READONLY_BROKER_SYMBOLS, force=True)
        self.assertEqual(trailing.exception.code, "FUTU_BROKER_OUTPUT_INVALID")

        stderr_program = _worker_program(
            managed=False,
            stderr_prefix="UNTRUSTED_SDK_STDERR",
        )
        captured_stderr = io.StringIO()
        with (
            patch.object(
                broker_module,
                "_worker_command",
                _command_for(stderr_program),
            ),
            contextlib.redirect_stderr(captured_stderr),
        ):
            broker = FutuReadOnlyBroker(mode="one_shot")
            snapshot = broker.quote_batch(FUTU_READONLY_BROKER_SYMBOLS, force=True)
        self.assertEqual(snapshot["symbols"], list(FUTU_READONLY_BROKER_SYMBOLS))
        self.assertEqual(captured_stderr.getvalue(), "")

    def test_crashed_managed_worker_restarts_on_next_request_only(self) -> None:
        crashing = "import sys; sys.stdin.buffer.readline(); raise SystemExit(7)"
        selected = {"program": crashing}

        def command(_mode: str) -> list[str]:
            return [sys.executable, "-I", "-B", "-c", selected["program"]]

        with patch.object(broker_module, "_worker_command", command):
            broker = FutuReadOnlyBroker(mode="managed")
            with self.assertRaises(FutuReadOnlyBrokerError):
                broker.quote_batch(FUTU_READONLY_BROKER_SYMBOLS, force=True)
            self.assertEqual(broker._request_count, 0)
            self.assertIsNone(broker._process)

            selected["program"] = _worker_program(managed=True)
            snapshot = broker.quote_batch(FUTU_READONLY_BROKER_SYMBOLS, force=True)
            self.assertEqual(snapshot["symbols"], list(FUTU_READONLY_BROKER_SYMBOLS))
            self.assertEqual(broker._request_count, 1)
            self.assertTrue(broker.stop())


if __name__ == "__main__":
    unittest.main()
