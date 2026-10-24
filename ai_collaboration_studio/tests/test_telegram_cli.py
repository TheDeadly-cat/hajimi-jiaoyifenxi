from __future__ import annotations

import io
import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.telegram_intelligence import cli as telegram_cli
from backend.telegram_intelligence.cli import (
    CLICommandError,
    build_tdlib_client,
    main,
    read_local_status,
)
from backend.telegram_intelligence.settings import TelegramSettings
from backend.telegram_intelligence.tdlib import TDLibChat


def _settings(**overrides: str) -> TelegramSettings:
    values = {
        "AI_STUDIO_SKIP_LOCAL_ENV": "1",
        "TELEGRAM_API_ID": "12345",
        "TELEGRAM_API_HASH": "api-hash-value",
        "TELEGRAM_PHONE": "+15550001111",
        "TELEGRAM_TDLIB_LIBRARY_PATH": "C:/fake/tdjson.dll",
        "TELEGRAM_HEALTH_HOST": "127.0.0.1",
        "TELEGRAM_HEALTH_PORT": "8781",
        **overrides,
    }
    return TelegramSettings.from_env(values, load_local=False)


class _Client:
    def __init__(self):
        self.started = False
        self.closed = False
        self.authorization_state = "authorizationStateReady"
        self.connection_state = "connectionStateReady"

    def start(self):
        self.started = True

    def wait_ready(self, timeout):
        return True

    def list_chats(self, *, limit, timeout):
        return (
            TDLibChat(-1001, "Official", "supergroup"),
            TDLibChat(2, "Private", "private"),
        )[:limit]

    def close(self):
        self.closed = True


class TelegramCLITests(unittest.TestCase):
    def test_optional_telegram_prefix_login_and_chats(self) -> None:
        login_client = _Client()
        output = io.StringIO()
        code = main(
            ["telegram", "login", "--timeout", "1"],
            settings=_settings(),
            client_factory=lambda settings: login_client,
            stdout=output,
        )
        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertTrue(login_client.started)
        self.assertTrue(login_client.closed)
        self.assertEqual(payload["authorization_state"], "authorizationStateReady")

        chats_client = _Client()
        output = io.StringIO()
        code = main(
            ["chats", "--limit", "1"],
            settings=_settings(),
            client_factory=lambda settings: chats_client,
            stdout=output,
        )
        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["chats"][0]["chat_id"], -1001)
        self.assertTrue(chats_client.closed)

    def test_run_and_history_import_runtime_lazily_with_exact_arguments(self) -> None:
        calls = []

        def run_service(settings):
            calls.append(("run", settings))
            return {"state": "stopped"}

        def run_history(settings, **kwargs):
            calls.append(("history", settings, kwargs))
            return {
                "chat_id": kwargs["chat_id"],
                "processed_count": 3,
            }

        runtime = SimpleNamespace(
            run_service=run_service,
            run_history=run_history,
        )
        settings = _settings()
        output = io.StringIO()
        self.assertEqual(
            main(
                ["run"],
                settings=settings,
                runtime_loader=lambda: runtime,
                stdout=output,
            ),
            0,
        )
        output = io.StringIO()
        self.assertEqual(
            main(
                [
                    "telegram",
                    "history",
                    "--chat-id",
                    "-1001",
                    "--limit",
                    "3",
                    "--since-time",
                    "2026-08-29T12:00:00+08:00",
                ],
                settings=settings,
                runtime_loader=lambda: runtime,
                stdout=output,
            ),
            0,
        )
        history_call = calls[1]
        self.assertEqual(history_call[2]["chat_id"], -1001)
        self.assertEqual(history_call[2]["limit"], 3)
        self.assertEqual(
            history_call[2]["since_time"].isoformat(),
            "2026-08-29T04:00:00+00:00",
        )

    def test_status_uses_injected_loopback_reader_and_redacts_sensitive_keys(self) -> None:
        output = io.StringIO()

        def reader(settings, *, timeout):
            return {
                "authorization_state": "authorizationStateReady",
                "queue_size": 2,
                "api_hash": "never-output",
                "database_path": "C:/private",
                "message_content": "private body",
            }

        code = main(
            ["status", "--timeout", "1"],
            settings=_settings(),
            status_reader=reader,
            stdout=output,
        )
        serialized = output.getvalue()
        payload = json.loads(serialized)
        self.assertEqual(code, 0)
        self.assertEqual(
            payload["result"]["authorization_state"],
            "authorizationStateReady",
        )
        self.assertNotIn("never-output", serialized)
        self.assertNotIn("C:/private", serialized)
        self.assertNotIn("private body", serialized)

    def test_local_status_rejects_redirect_through_no_redirect_opener(self) -> None:
        handler = next(
            item
            for item in telegram_cli._LOCAL_STATUS_OPENER.handlers
            if isinstance(item, telegram_cli._NoRedirectHandler)
        )
        request = urllib.request.Request("http://127.0.0.1:8781/status")
        self.assertIsNone(
            handler.redirect_request(
                request,
                None,
                302,
                "Found",
                {"Location": "http://127.0.0.1:8782/status"},
                "http://127.0.0.1:8782/status",
            )
        )
        redirect = urllib.error.HTTPError(
            request.full_url,
            302,
            "Found",
            {"Location": "http://127.0.0.1:8782/status"},
            None,
        )
        self.addCleanup(redirect.close)
        with (
            patch.object(
                telegram_cli._LOCAL_STATUS_OPENER,
                "open",
                side_effect=redirect,
            ) as opener,
            patch.object(
                urllib.request,
                "urlopen",
                side_effect=AssertionError("redirect fallback must stay offline"),
            ) as urlopen,
        ):
            with self.assertRaisesRegex(CLICommandError, "unavailable"):
                read_local_status(_settings(), timeout=1.0)

        opened_request = opener.call_args.args[0]
        self.assertEqual(opened_request.full_url, request.full_url)
        opener.assert_called_once()
        urlopen.assert_not_called()

    def test_runtime_error_is_safe_json_and_does_not_echo_secret(self) -> None:
        def fail(settings):
            raise RuntimeError("bot_token=super-secret-value at C:/private/session")

        output = io.StringIO()
        code = main(
            ["run"],
            settings=_settings(),
            runtime_loader=lambda: SimpleNamespace(run_service=fail),
            stdout=output,
        )
        serialized = output.getvalue()
        payload = json.loads(serialized)
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["message"], "command failed")
        self.assertNotIn("super-secret", serialized)
        self.assertNotIn("C:/private", serialized)

    def test_parser_rejects_naive_since_time_and_invalid_limit_as_json(self) -> None:
        output = io.StringIO()
        code = main(
            [
                "history",
                "--chat-id",
                "-1",
                "--since-time",
                "2026-08-29T12:00:00",
            ],
            settings=_settings(),
            runtime_loader=lambda: None,
            stdout=output,
        )
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue())["error"]["type"], "CLIUsageError")

        output = io.StringIO()
        code = main(
            ["chats", "--limit", "0"],
            settings=_settings(),
            client_factory=lambda settings: _Client(),
            stdout=output,
        )
        self.assertEqual(code, 2)

    def test_build_client_uses_getpass_only_for_code_and_2fa(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            library = Path(directory) / "tdjson.dll"
            library.write_bytes(b"offline-test-double")
            settings = _settings(
                TELEGRAM_TDLIB_LIBRARY_PATH=str(library),
                TELEGRAM_TDLIB_DATABASE_DIR=str(Path(directory) / "db"),
                TELEGRAM_TDLIB_FILES_DIR=str(Path(directory) / "files"),
                TELEGRAM_DATABASE_PATH=str(Path(directory) / "events.sqlite3"),
            )
            prompts = []

            def secret_prompt(prompt):
                prompts.append(prompt)
                return "12345" if "code" in prompt else "2fa-secret"

            captured = {}

            def fake_client(transport, td_settings, *, auth):
                captured["transport"] = transport
                captured["settings"] = td_settings
                captured["auth"] = auth
                return "client"

            with (
                patch(
                    "backend.telegram_intelligence.cli.CTypesTDLibTransport",
                    return_value="transport",
                ),
                patch(
                    "backend.telegram_intelligence.cli.TDLibClient",
                    side_effect=fake_client,
                ),
            ):
                result = build_tdlib_client(settings, getpass_fn=secret_prompt)
            self.assertEqual(result, "client")
            self.assertEqual(captured["auth"].phone_number(), "+15550001111")
            self.assertEqual(captured["auth"].authentication_code(), "12345")
            self.assertEqual(captured["auth"].password(), "2fa-secret")
            self.assertEqual(
                prompts,
                ["Telegram login code: ", "Telegram 2FA password: "],
            )


if __name__ == "__main__":
    unittest.main()
