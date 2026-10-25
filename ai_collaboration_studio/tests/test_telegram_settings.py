from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.telegram_intelligence.settings import (
    DEFAULT_MAX_DATABASE_BYTES,
    MAX_MAX_DATABASE_BYTES,
    MIN_MAX_DATABASE_BYTES,
    TelegramSettings,
    TelegramSettingsError,
)


class TelegramSettingsTests(unittest.TestCase):
    def test_defaults_are_dry_run_and_media_download_is_off(self) -> None:
        settings = TelegramSettings.from_env(
            {"AI_STUDIO_SKIP_LOCAL_ENV": "1"},
            load_local=False,
        )

        self.assertTrue(settings.dry_run)
        self.assertFalse(settings.enable_ai)
        self.assertFalse(settings.enable_alerts)
        self.assertFalse(settings.download_media)
        self.assertEqual(settings.history_max_limit, 1_000)
        self.assertEqual(settings.max_database_bytes, DEFAULT_MAX_DATABASE_BYTES)
        self.assertEqual(settings.shutdown_timeout_seconds, 90)
        self.assertEqual(settings.timezone_name, "Asia/Shanghai")
        self.assertNotIn("api_hash", repr(settings))
        self.assertNotIn("bot_token", repr(settings))
        self.assertNotIn("phone=", repr(settings))
        self.assertNotIn("encryption_key", repr(settings))

    def test_tdlib_database_encryption_key_is_loaded_but_never_represented(self) -> None:
        settings = TelegramSettings.from_env(
            {
                "AI_STUDIO_SKIP_LOCAL_ENV": "1",
                "TELEGRAM_TDLIB_DATABASE_ENCRYPTION_KEY": "synthetic-local-key",
            },
            load_local=False,
        )

        self.assertEqual(
            settings.tdlib_database_encryption_key,
            "synthetic-local-key",
        )
        self.assertNotIn("synthetic-local-key", repr(settings))

    def test_rejects_live_alerts_without_fixed_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            library = Path(temp_dir) / "tdjson.dll"
            library.write_bytes(b"synthetic")
            settings = TelegramSettings.from_env(
                {
                    "AI_STUDIO_SKIP_LOCAL_ENV": "1",
                    "TELEGRAM_API_ID": "123",
                    "TELEGRAM_API_HASH": "synthetic",
                    "TELEGRAM_PHONE": "+10000000000",
                    "TELEGRAM_TDLIB_LIBRARY_PATH": str(library),
                    "ENABLE_ALERTS": "true",
                    "DRY_RUN": "false",
                },
                load_local=False,
            )
            with self.assertRaisesRegex(
                TelegramSettingsError,
                "fixed numeric destination",
            ):
                settings.validate_runtime(require_alerts=True)

    def test_rejects_non_loopback_health_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            library = Path(temp_dir) / "tdjson.dll"
            library.write_bytes(b"synthetic")
            settings = TelegramSettings.from_env(
                {
                    "AI_STUDIO_SKIP_LOCAL_ENV": "1",
                    "TELEGRAM_API_ID": "123",
                    "TELEGRAM_API_HASH": "synthetic",
                    "TELEGRAM_PHONE": "+10000000000",
                    "TELEGRAM_TDLIB_LIBRARY_PATH": str(library),
                    "TELEGRAM_HEALTH_HOST": "0.0.0.0",
                },
                load_local=False,
            )
            with self.assertRaisesRegex(TelegramSettingsError, "loopback"):
                settings.validate_runtime()

    def test_rejects_zero_alert_cooldown(self) -> None:
        with self.assertRaisesRegex(TelegramSettingsError, "between 1 and 86400"):
            TelegramSettings.from_env(
                {
                    "AI_STUDIO_SKIP_LOCAL_ENV": "1",
                    "TELEGRAM_ALERT_COOLDOWN_SECONDS": "0",
                },
                load_local=False,
            )

    def test_database_capacity_limit_is_bounded(self) -> None:
        configured = TelegramSettings.from_env(
            {
                "AI_STUDIO_SKIP_LOCAL_ENV": "1",
                "TELEGRAM_MAX_DATABASE_BYTES": str(MIN_MAX_DATABASE_BYTES),
            },
            load_local=False,
        )
        self.assertEqual(configured.max_database_bytes, MIN_MAX_DATABASE_BYTES)
        for value in (MIN_MAX_DATABASE_BYTES - 1, MAX_MAX_DATABASE_BYTES + 1):
            with self.subTest(value=value), self.assertRaisesRegex(
                TelegramSettingsError,
                "TELEGRAM_MAX_DATABASE_BYTES must be between",
            ):
                TelegramSettings.from_env(
                    {
                        "AI_STUDIO_SKIP_LOCAL_ENV": "1",
                        "TELEGRAM_MAX_DATABASE_BYTES": str(value),
                    },
                    load_local=False,
                )

    def test_shutdown_timeout_is_bounded(self) -> None:
        configured = TelegramSettings.from_env(
            {
                "AI_STUDIO_SKIP_LOCAL_ENV": "1",
                "TELEGRAM_SHUTDOWN_TIMEOUT_SECONDS": "120",
            },
            load_local=False,
        )
        self.assertEqual(configured.shutdown_timeout_seconds, 120)
        for value in (4, 601):
            with self.subTest(value=value), self.assertRaisesRegex(
                TelegramSettingsError,
                "TELEGRAM_SHUTDOWN_TIMEOUT_SECONDS must be between",
            ):
                TelegramSettings.from_env(
                    {
                        "AI_STUDIO_SKIP_LOCAL_ENV": "1",
                        "TELEGRAM_SHUTDOWN_TIMEOUT_SECONDS": str(value),
                    },
                    load_local=False,
                )


if __name__ == "__main__":
    unittest.main()
