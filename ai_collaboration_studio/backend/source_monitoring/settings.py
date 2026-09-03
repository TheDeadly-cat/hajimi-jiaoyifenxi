"""Strict environment settings for the disabled-by-default monitor."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .contracts import MAX_OBSERVED_ITEMS_PER_POLL, SourceMonitoringContractError


SOURCE_MONITOR_ENABLED_ENV = "AI_STUDIO_SOURCE_MONITOR_ENABLED"
SOURCE_MONITOR_AUTO_START_ENV = "AI_STUDIO_SOURCE_MONITOR_AUTO_START"
SOURCE_MONITOR_OFFICIAL_ONLY_ENV = "AI_STUDIO_SOURCE_MONITOR_OFFICIAL_ONLY"
SOURCE_MONITOR_DRY_RUN_ENV = "AI_STUDIO_SOURCE_MONITOR_DRY_RUN"
SOURCE_MONITOR_MAX_ITEMS_ENV = "AI_STUDIO_SOURCE_MONITOR_MAX_ITEMS_PER_RUN"
SOURCE_MONITOR_TRADING_IMPACT_RULES_ENABLED_ENV = (
    "AI_STUDIO_SOURCE_MONITOR_TRADING_IMPACT_RULES_ENABLED"
)
SOURCE_MONITOR_ALLOW_READONLY_MARKET_ENV = (
    "AI_STUDIO_SOURCE_MONITOR_ALLOW_READONLY_MARKET"
)
SOURCE_MONITOR_INITIAL_MODE_ENV = "AI_STUDIO_SOURCE_MONITOR_INITIAL_MODE"
SOURCE_MONITOR_CATCH_UP_MAX_ITEMS_ENV = (
    "AI_STUDIO_SOURCE_MONITOR_CATCH_UP_MAX_ITEMS"
)
SOURCE_MONITOR_INITIAL_PREVIEW_SHA256_ENV = (
    "AI_STUDIO_SOURCE_MONITOR_INITIAL_PREVIEW_SHA256"
)
SOURCE_MONITOR_FROM_TIME_ENV = "AI_STUDIO_SOURCE_MONITOR_FROM_TIME"

SOURCE_MONITOR_INITIAL_MODES = ("seed_only", "catch_up", "from_time")

_INTEGER_RE = re.compile(r"[1-9][0-9]*\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_RFC3339_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.([0-9]{1,9}))?"
    r"(?:Z|[+-][0-9]{2}:[0-9]{2})\Z"
)


class SourceMonitoringSettingsError(SourceMonitoringContractError):
    """Raised for explicit but malformed monitor environment settings."""


def _environment_value(
    environment: Mapping[str, str],
    name: str,
) -> str | None:
    value = environment.get(name)
    if value is None:
        return None
    if type(value) is not str:
        raise SourceMonitoringSettingsError(
            "SOURCE_MONITORING_SETTING_TYPE_INVALID",
            f"{name} must be a native string",
        )
    return value


def _strict_boolean(
    environment: Mapping[str, str],
    name: str,
    *,
    default: bool,
) -> bool:
    raw = _environment_value(environment, name)
    if raw is None:
        return default
    if raw == "0":
        return False
    if raw == "1":
        return True
    raise SourceMonitoringSettingsError(
        "SOURCE_MONITORING_BOOLEAN_INVALID",
        f"{name} must be exactly 0 or 1",
    )


def _strict_max_items(environment: Mapping[str, str]) -> int:
    raw = _environment_value(environment, SOURCE_MONITOR_MAX_ITEMS_ENV)
    if raw is None:
        return MAX_OBSERVED_ITEMS_PER_POLL
    if not _INTEGER_RE.fullmatch(raw):
        raise SourceMonitoringSettingsError(
            "SOURCE_MONITORING_MAX_ITEMS_INVALID",
            f"{SOURCE_MONITOR_MAX_ITEMS_ENV} must be a canonical positive integer",
        )
    value = int(raw)
    if not 1 <= value <= MAX_OBSERVED_ITEMS_PER_POLL:
        raise SourceMonitoringSettingsError(
            "SOURCE_MONITORING_MAX_ITEMS_INVALID",
            f"{SOURCE_MONITOR_MAX_ITEMS_ENV} must be between 1 and 50",
        )
    return value


def _strict_initial_mode(environment: Mapping[str, str]) -> str:
    raw = _environment_value(environment, SOURCE_MONITOR_INITIAL_MODE_ENV)
    if raw is None:
        return "seed_only"
    if raw not in SOURCE_MONITOR_INITIAL_MODES:
        raise SourceMonitoringSettingsError(
            "SOURCE_MONITORING_INITIAL_MODE_INVALID",
            (
                f"{SOURCE_MONITOR_INITIAL_MODE_ENV} must be exactly one of "
                "seed_only, catch_up, or from_time"
            ),
        )
    return raw


def _strict_optional_positive_integer(
    environment: Mapping[str, str],
    name: str,
) -> int:
    raw = _environment_value(environment, name)
    if raw is None:
        return 0
    if not _INTEGER_RE.fullmatch(raw):
        raise SourceMonitoringSettingsError(
            "SOURCE_MONITORING_INITIAL_LIMIT_INVALID",
            f"{name} must be a canonical positive integer",
        )
    value = int(raw)
    if not 1 <= value <= MAX_OBSERVED_ITEMS_PER_POLL:
        raise SourceMonitoringSettingsError(
            "SOURCE_MONITORING_INITIAL_LIMIT_INVALID",
            f"{name} must be between 1 and 50",
        )
    return value


def _strict_optional_sha256(
    environment: Mapping[str, str],
    name: str,
) -> str:
    raw = _environment_value(environment, name)
    if raw is None:
        return ""
    if _SHA256_RE.fullmatch(raw) is None:
        raise SourceMonitoringSettingsError(
            "SOURCE_MONITORING_INITIAL_PREVIEW_INVALID",
            f"{name} must be a lowercase SHA-256 digest",
        )
    return raw


def _normalize_from_time(value: Any, *, required: bool) -> str:
    if value == "" and not required:
        return ""
    if type(value) is not str:
        raise SourceMonitoringSettingsError(
            "SOURCE_MONITORING_FROM_TIME_INVALID",
            "from_time must be a native RFC3339 string",
        )
    match = _RFC3339_RE.fullmatch(value)
    if match is None:
        raise SourceMonitoringSettingsError(
            "SOURCE_MONITORING_FROM_TIME_INVALID",
            "from_time must be an explicit timezone-aware RFC3339 timestamp",
        )
    fraction = match.group(1) or ""
    if len(fraction) > 3 and any(character != "0" for character in fraction[3:]):
        raise SourceMonitoringSettingsError(
            "SOURCE_MONITORING_FROM_TIME_PRECISION_INVALID",
            "from_time cannot contain non-zero sub-millisecond precision",
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SourceMonitoringSettingsError(
            "SOURCE_MONITORING_FROM_TIME_INVALID",
            "from_time is not a valid RFC3339 timestamp",
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SourceMonitoringSettingsError(
            "SOURCE_MONITORING_FROM_TIME_INVALID",
            "from_time must include a timezone",
        )
    utc = parsed.astimezone(timezone.utc)
    if utc < datetime(1970, 1, 1, tzinfo=timezone.utc):
        raise SourceMonitoringSettingsError(
            "SOURCE_MONITORING_FROM_TIME_INVALID",
            "from_time must not be earlier than the Unix epoch",
        )
    milliseconds = utc.microsecond // 1_000
    base = utc.strftime("%Y-%m-%dT%H:%M:%S")
    return f"{base}.{milliseconds:03d}Z" if milliseconds else f"{base}Z"


@dataclass(frozen=True, slots=True)
class SourceMonitoringSettings:
    enabled: bool = False
    auto_start: bool = False
    official_only: bool = True
    allow_readonly_market: bool = False
    dry_run: bool = True
    max_items_per_run: int = MAX_OBSERVED_ITEMS_PER_POLL
    trading_impact_rules_enabled: bool = False
    initial_mode: str = "seed_only"
    catch_up_max_items: int = 0
    initial_preview_sha256: str = ""
    from_time: str = ""

    def __post_init__(self) -> None:
        for field_name in (
            "enabled",
            "auto_start",
            "official_only",
            "allow_readonly_market",
            "trading_impact_rules_enabled",
            "dry_run",
        ):
            if type(getattr(self, field_name)) is not bool:
                raise SourceMonitoringSettingsError(
                    "SOURCE_MONITORING_SETTING_TYPE_INVALID",
                    f"{field_name} must be a native boolean",
                )
        if (
            type(self.max_items_per_run) is not int
            or not 1
            <= self.max_items_per_run
            <= MAX_OBSERVED_ITEMS_PER_POLL
        ):
            raise SourceMonitoringSettingsError(
                "SOURCE_MONITORING_MAX_ITEMS_INVALID",
                "max_items_per_run must be a native integer between 1 and 50",
            )
        if self.official_only is self.allow_readonly_market:
            raise SourceMonitoringSettingsError(
                "SOURCE_MONITORING_SOURCE_MODE_INVALID",
                (
                    "exactly one of official_only or allow_readonly_market "
                    "must be true"
                ),
            )
        if self.auto_start and not self.enabled:
            raise SourceMonitoringSettingsError(
                "SOURCE_MONITORING_SETTING_CONFLICT",
                "auto_start cannot be enabled while monitoring is disabled",
            )
        if type(self.initial_mode) is not str or self.initial_mode not in (
            SOURCE_MONITOR_INITIAL_MODES
        ):
            raise SourceMonitoringSettingsError(
                "SOURCE_MONITORING_INITIAL_MODE_INVALID",
                "initial_mode must be seed_only, catch_up, or from_time",
            )
        if (
            type(self.catch_up_max_items) is not int
            or not 0 <= self.catch_up_max_items <= MAX_OBSERVED_ITEMS_PER_POLL
        ):
            raise SourceMonitoringSettingsError(
                "SOURCE_MONITORING_INITIAL_LIMIT_INVALID",
                "catch_up_max_items must be a native integer between 0 and 50",
            )
        if (
            type(self.initial_preview_sha256) is not str
            or (
                self.initial_preview_sha256 != ""
                and _SHA256_RE.fullmatch(self.initial_preview_sha256) is None
            )
        ):
            raise SourceMonitoringSettingsError(
                "SOURCE_MONITORING_INITIAL_PREVIEW_INVALID",
                "initial_preview_sha256 must be empty or a lowercase SHA-256 digest",
            )
        clean_from_time = _normalize_from_time(
            self.from_time,
            required=self.initial_mode == "from_time",
        )
        object.__setattr__(self, "from_time", clean_from_time)
        if self.initial_mode == "seed_only" and (
            self.catch_up_max_items != 0
            or self.initial_preview_sha256
            or self.from_time
        ):
            raise SourceMonitoringSettingsError(
                "SOURCE_MONITORING_INITIAL_SETTING_CONFLICT",
                "seed_only cannot include catch-up or from-time settings",
            )
        if self.initial_mode == "catch_up":
            if not 1 <= self.catch_up_max_items <= self.max_items_per_run:
                raise SourceMonitoringSettingsError(
                    "SOURCE_MONITORING_INITIAL_LIMIT_REQUIRED",
                    (
                        "catch_up requires an explicit positive maximum no greater "
                        "than max_items_per_run"
                    ),
                )
            if self.from_time:
                raise SourceMonitoringSettingsError(
                    "SOURCE_MONITORING_INITIAL_SETTING_CONFLICT",
                    "catch_up cannot include from_time",
                )
        if self.initial_mode == "from_time" and (
            self.catch_up_max_items != 0 or self.initial_preview_sha256
        ):
            raise SourceMonitoringSettingsError(
                "SOURCE_MONITORING_INITIAL_SETTING_CONFLICT",
                "from_time cannot include catch-up settings",
            )

    @property
    def from_time_ms(self) -> int:
        if not self.from_time:
            return 0
        parsed = datetime.fromisoformat(self.from_time.replace("Z", "+00:00"))
        return int(parsed.timestamp() * 1_000)

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> "SourceMonitoringSettings":
        source = os.environ if environment is None else environment
        if not isinstance(source, Mapping):
            raise SourceMonitoringSettingsError(
                "SOURCE_MONITORING_ENVIRONMENT_INVALID",
                "environment must be a mapping",
            )
        initial_mode = _strict_initial_mode(source)
        catch_up_max_items = _strict_optional_positive_integer(
            source,
            SOURCE_MONITOR_CATCH_UP_MAX_ITEMS_ENV,
        )
        initial_preview_sha256 = _strict_optional_sha256(
            source,
            SOURCE_MONITOR_INITIAL_PREVIEW_SHA256_ENV,
        )
        raw_from_time = _environment_value(source, SOURCE_MONITOR_FROM_TIME_ENV)
        return cls(
            enabled=_strict_boolean(
                source,
                SOURCE_MONITOR_ENABLED_ENV,
                default=False,
            ),
            auto_start=_strict_boolean(
                source,
                SOURCE_MONITOR_AUTO_START_ENV,
                default=False,
            ),
            official_only=_strict_boolean(
                source,
                SOURCE_MONITOR_OFFICIAL_ONLY_ENV,
                default=True,
            ),
            allow_readonly_market=_strict_boolean(
                source,
                SOURCE_MONITOR_ALLOW_READONLY_MARKET_ENV,
                default=False,
            ),
            trading_impact_rules_enabled=_strict_boolean(
                source,
                SOURCE_MONITOR_TRADING_IMPACT_RULES_ENABLED_ENV,
                default=False,
            ),
            dry_run=_strict_boolean(
                source,
                SOURCE_MONITOR_DRY_RUN_ENV,
                default=True,
            ),
            max_items_per_run=_strict_max_items(source),
            initial_mode=initial_mode,
            catch_up_max_items=catch_up_max_items,
            initial_preview_sha256=initial_preview_sha256,
            from_time=("" if raw_from_time is None else raw_from_time),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "auto_start": self.auto_start,
            "official_only": self.official_only,
            "allow_readonly_market": self.allow_readonly_market,
            "trading_impact_rules_enabled": self.trading_impact_rules_enabled,
            "dry_run": self.dry_run,
            "max_items_per_run": self.max_items_per_run,
            "initial_mode": self.initial_mode,
            "catch_up_max_items": self.catch_up_max_items,
            "initial_preview_sha256": self.initial_preview_sha256,
            "from_time": self.from_time,
        }


def load_source_monitoring_settings(
    environment: Mapping[str, str] | None = None,
) -> SourceMonitoringSettings:
    return SourceMonitoringSettings.from_environment(environment)


__all__ = [
    "SOURCE_MONITOR_AUTO_START_ENV",
    "SOURCE_MONITOR_ALLOW_READONLY_MARKET_ENV",
    "SOURCE_MONITOR_CATCH_UP_MAX_ITEMS_ENV",
    "SOURCE_MONITOR_DRY_RUN_ENV",
    "SOURCE_MONITOR_ENABLED_ENV",
    "SOURCE_MONITOR_FROM_TIME_ENV",
    "SOURCE_MONITOR_INITIAL_MODE_ENV",
    "SOURCE_MONITOR_INITIAL_MODES",
    "SOURCE_MONITOR_INITIAL_PREVIEW_SHA256_ENV",
    "SOURCE_MONITOR_MAX_ITEMS_ENV",
    "SOURCE_MONITOR_OFFICIAL_ONLY_ENV",
    "SOURCE_MONITOR_TRADING_IMPACT_RULES_ENABLED_ENV",
    "SourceMonitoringSettings",
    "SourceMonitoringSettingsError",
    "load_source_monitoring_settings",
]
