from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_MAX_DATABASE_BYTES = 4 * 1024 * 1024 * 1024
MIN_MAX_DATABASE_BYTES = 128 * 1024 * 1024
MAX_MAX_DATABASE_BYTES = 1024 * 1024 * 1024 * 1024


class TelegramSettingsError(ValueError):
    """Raised before any network or database action for invalid configuration."""


def _bool_value(values: Mapping[str, str], name: str, default: bool) -> bool:
    raw = str(values.get(name, "")).strip().casefold()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise TelegramSettingsError(f"{name} must be true or false")


def _int_value(
    values: Mapping[str, str],
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    raw = str(values.get(name, "")).strip()
    if not raw:
        return default
    try:
        result = int(raw)
    except ValueError as exc:
        raise TelegramSettingsError(f"{name} must be an integer") from exc
    if not minimum <= result <= maximum:
        raise TelegramSettingsError(
            f"{name} must be between {minimum} and {maximum}"
        )
    return result


def _path_value(values: Mapping[str, str], name: str, default: Path) -> Path:
    raw = str(values.get(name, "")).strip()
    return Path(raw).expanduser().resolve() if raw else default.resolve()


def _load_project_env(base: Mapping[str, str]) -> dict[str, str]:
    """Load only declared Telegram settings without mutating process env."""

    values = dict(base)
    if str(values.get("AI_STUDIO_SKIP_LOCAL_ENV", "")).strip().casefold() in {
        "1",
        "true",
        "yes",
    }:
        return values
    path = PROJECT_DIR / ".env.local"
    if not path.is_file():
        return values
    allowed = {
        "TELEGRAM_API_ID",
        "TELEGRAM_API_HASH",
        "TELEGRAM_PHONE",
        "TELEGRAM_TDLIB_LIBRARY_PATH",
        "TELEGRAM_TDLIB_DATABASE_ENCRYPTION_KEY",
        "TELEGRAM_TDLIB_DATABASE_DIR",
        "TELEGRAM_TDLIB_FILES_DIR",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_DESTINATION_CHAT_ID",
        "TELEGRAM_CHATS_CONFIG",
        "TELEGRAM_KEYWORDS_CONFIG",
        "TELEGRAM_DATABASE_PATH",
        "TELEGRAM_HEALTH_HOST",
        "TELEGRAM_HEALTH_PORT",
        "TELEGRAM_HISTORY_MAX_LIMIT",
        "TELEGRAM_MAX_DATABASE_BYTES",
        "TELEGRAM_SHUTDOWN_TIMEOUT_SECONDS",
        "TELEGRAM_MAX_MEDIA_BYTES",
        "TELEGRAM_PRE_SCORE_LIGHT_THRESHOLD",
        "TELEGRAM_PRE_SCORE_FULL_THRESHOLD",
        "TELEGRAM_ALERT_COOLDOWN_SECONDS",
        "TELEGRAM_ANALYZER_PROVIDER",
        "TELEGRAM_AI_INPUT_USD_PER_MILLION",
        "TELEGRAM_AI_OUTPUT_USD_PER_MILLION",
        "ENABLE_AI",
        "ENABLE_ALERTS",
        "DOWNLOAD_MEDIA",
        "DRY_RUN",
        "APP_ENV",
        "LOG_LEVEL",
        "TIMEZONE",
    }
    try:
        for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, raw_value = line.split("=", 1)
            clean_name = name.strip()
            if clean_name not in allowed or clean_name in values:
                continue
            clean_value = raw_value.strip().strip('"').strip("'")
            if clean_value:
                values[clean_name] = clean_value
    except OSError as exc:
        raise TelegramSettingsError("unable to read .env.local") from exc
    return values


@dataclass(frozen=True, slots=True)
class TelegramSettings:
    api_id: int
    api_hash: str = field(repr=False)
    phone: str = field(repr=False)
    tdlib_library_path: Path | None
    tdlib_database_dir: Path
    tdlib_files_dir: Path
    bot_token: str = field(repr=False)
    destination_chat_id: str
    chats_config_path: Path
    keywords_config_path: Path
    database_path: Path
    health_host: str = "127.0.0.1"
    health_port: int = 8781
    history_max_limit: int = 1_000
    max_database_bytes: int = DEFAULT_MAX_DATABASE_BYTES
    shutdown_timeout_seconds: int = 90
    max_media_bytes: int = 20 * 1024 * 1024
    pre_score_light_threshold: int = 30
    pre_score_full_threshold: int = 60
    alert_cooldown_seconds: int = 300
    analyzer_provider: str = "openai"
    enable_ai: bool = False
    enable_alerts: bool = False
    download_media: bool = False
    dry_run: bool = True
    app_env: str = "development"
    log_level: str = "INFO"
    timezone_name: str = "Asia/Shanghai"
    ai_input_usd_per_million: float | None = None
    ai_output_usd_per_million: float | None = None
    tdlib_database_encryption_key: str = field(default="", repr=False)

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        load_local: bool = True,
    ) -> "TelegramSettings":
        base = dict(os.environ if environ is None else environ)
        values = _load_project_env(base) if load_local else base
        runtime = _path_value(
            values,
            "AI_STUDIO_RUNTIME_DIR",
            PROJECT_DIR / "runtime",
        )
        api_id = _int_value(
            values,
            "TELEGRAM_API_ID",
            0,
            minimum=0,
            maximum=2_147_483_647,
        )
        library_raw = str(values.get("TELEGRAM_TDLIB_LIBRARY_PATH", "")).strip()

        def optional_rate(name: str) -> float | None:
            raw = str(values.get(name, "")).strip()
            if not raw:
                return None
            try:
                rate = float(raw)
            except ValueError as exc:
                raise TelegramSettingsError(f"{name} must be numeric") from exc
            if not 0 <= rate <= 1_000_000:
                raise TelegramSettingsError(f"{name} is outside the allowed range")
            return rate

        return cls(
            api_id=api_id,
            api_hash=str(values.get("TELEGRAM_API_HASH", "")).strip(),
            phone=str(values.get("TELEGRAM_PHONE", "")).strip(),
            tdlib_library_path=(
                Path(library_raw).expanduser().resolve() if library_raw else None
            ),
            tdlib_database_dir=_path_value(
                values,
                "TELEGRAM_TDLIB_DATABASE_DIR",
                runtime / "telegram" / "tdlib-data",
            ),
            tdlib_files_dir=_path_value(
                values,
                "TELEGRAM_TDLIB_FILES_DIR",
                runtime / "telegram" / "files",
            ),
            bot_token=str(values.get("TELEGRAM_BOT_TOKEN", "")).strip(),
            destination_chat_id=str(
                values.get("TELEGRAM_DESTINATION_CHAT_ID", "")
            ).strip(),
            chats_config_path=_path_value(
                values,
                "TELEGRAM_CHATS_CONFIG",
                PROJECT_DIR / "config" / "chats.yaml",
            ),
            keywords_config_path=_path_value(
                values,
                "TELEGRAM_KEYWORDS_CONFIG",
                PROJECT_DIR / "config" / "keywords.yaml",
            ),
            database_path=_path_value(
                values,
                "TELEGRAM_DATABASE_PATH",
                runtime / "telegram" / "telegram.sqlite3",
            ),
            health_host=str(
                values.get("TELEGRAM_HEALTH_HOST", "127.0.0.1")
            ).strip(),
            health_port=_int_value(
                values,
                "TELEGRAM_HEALTH_PORT",
                8781,
                minimum=1,
                maximum=65_535,
            ),
            history_max_limit=_int_value(
                values,
                "TELEGRAM_HISTORY_MAX_LIMIT",
                1_000,
                minimum=100,
                maximum=10_000,
            ),
            max_database_bytes=_int_value(
                values,
                "TELEGRAM_MAX_DATABASE_BYTES",
                DEFAULT_MAX_DATABASE_BYTES,
                minimum=MIN_MAX_DATABASE_BYTES,
                maximum=MAX_MAX_DATABASE_BYTES,
            ),
            shutdown_timeout_seconds=_int_value(
                values,
                "TELEGRAM_SHUTDOWN_TIMEOUT_SECONDS",
                90,
                minimum=5,
                maximum=600,
            ),
            max_media_bytes=_int_value(
                values,
                "TELEGRAM_MAX_MEDIA_BYTES",
                20 * 1024 * 1024,
                minimum=1,
                maximum=2 * 1024 * 1024 * 1024,
            ),
            pre_score_light_threshold=_int_value(
                values,
                "TELEGRAM_PRE_SCORE_LIGHT_THRESHOLD",
                30,
                minimum=0,
                maximum=100,
            ),
            pre_score_full_threshold=_int_value(
                values,
                "TELEGRAM_PRE_SCORE_FULL_THRESHOLD",
                60,
                minimum=0,
                maximum=100,
            ),
            alert_cooldown_seconds=_int_value(
                values,
                "TELEGRAM_ALERT_COOLDOWN_SECONDS",
                300,
                minimum=1,
                maximum=86_400,
            ),
            analyzer_provider=str(
                values.get("TELEGRAM_ANALYZER_PROVIDER", "openai")
            ).strip().casefold()
            or "openai",
            enable_ai=_bool_value(values, "ENABLE_AI", False),
            enable_alerts=_bool_value(values, "ENABLE_ALERTS", False),
            download_media=_bool_value(values, "DOWNLOAD_MEDIA", False),
            dry_run=_bool_value(values, "DRY_RUN", True),
            app_env=str(values.get("APP_ENV", "development")).strip(),
            log_level=str(values.get("LOG_LEVEL", "INFO")).strip().upper(),
            timezone_name=str(values.get("TIMEZONE", "Asia/Shanghai")).strip(),
            ai_input_usd_per_million=optional_rate(
                "TELEGRAM_AI_INPUT_USD_PER_MILLION"
            ),
            ai_output_usd_per_million=optional_rate(
                "TELEGRAM_AI_OUTPUT_USD_PER_MILLION"
            ),
            tdlib_database_encryption_key=str(
                values.get("TELEGRAM_TDLIB_DATABASE_ENCRYPTION_KEY", "")
            ),
        )

    def validate_runtime(self, *, require_alerts: bool = False) -> None:
        if self.api_id <= 0:
            raise TelegramSettingsError("TELEGRAM_API_ID is required")
        if not self.api_hash:
            raise TelegramSettingsError("TELEGRAM_API_HASH is required")
        if not self.phone:
            raise TelegramSettingsError("TELEGRAM_PHONE is required")
        if self.tdlib_library_path is None:
            raise TelegramSettingsError("TELEGRAM_TDLIB_LIBRARY_PATH is required")
        if not self.tdlib_library_path.is_file():
            raise TelegramSettingsError("TDLib library file does not exist")
        if self.health_host not in {"127.0.0.1", "::1", "localhost"}:
            raise TelegramSettingsError("Telegram health server must bind loopback")
        if self.pre_score_full_threshold < self.pre_score_light_threshold:
            raise TelegramSettingsError(
                "full analysis threshold must be >= light analysis threshold"
            )
        try:
            ZoneInfo(self.timezone_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise TelegramSettingsError("TIMEZONE is not available") from exc
        if self.destination_chat_id and (
            not self.destination_chat_id.lstrip("-").isdigit()
            or self.destination_chat_id in {"0", "-0"}
        ):
            raise TelegramSettingsError(
                "Telegram alert destination must be a non-zero numeric chat ID"
            )
        if require_alerts and self.enable_alerts:
            if not self.destination_chat_id:
                raise TelegramSettingsError(
                    "A fixed numeric destination is required when alerts are enabled"
                )
            if not self.dry_run and not self.bot_token:
                raise TelegramSettingsError(
                    "Bot token is required when live alerts are enabled"
                )

    def ensure_runtime_directories(self) -> None:
        for path in {
            self.tdlib_database_dir,
            self.tdlib_files_dir,
            self.database_path.parent,
        }:
            path.mkdir(parents=True, exist_ok=True)
            if os.name == "posix":
                try:
                    path.chmod(0o700)
                except OSError as exc:
                    raise TelegramSettingsError(
                        "unable to restrict Telegram runtime directory permissions"
                    ) from exc


__all__ = [
    "DEFAULT_MAX_DATABASE_BYTES",
    "MAX_MAX_DATABASE_BYTES",
    "MIN_MAX_DATABASE_BYTES",
    "TelegramSettings",
    "TelegramSettingsError",
]
