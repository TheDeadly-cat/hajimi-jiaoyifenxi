"""Strict environment settings for the disabled-by-default monitor."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .contracts import MAX_OBSERVED_ITEMS_PER_POLL, SourceMonitoringContractError


SOURCE_MONITOR_ENABLED_ENV = "AI_STUDIO_SOURCE_MONITOR_ENABLED"
SOURCE_MONITOR_AUTO_START_ENV = "AI_STUDIO_SOURCE_MONITOR_AUTO_START"
SOURCE_MONITOR_OFFICIAL_ONLY_ENV = "AI_STUDIO_SOURCE_MONITOR_OFFICIAL_ONLY"
SOURCE_MONITOR_DRY_RUN_ENV = "AI_STUDIO_SOURCE_MONITOR_DRY_RUN"
SOURCE_MONITOR_MAX_ITEMS_ENV = "AI_STUDIO_SOURCE_MONITOR_MAX_ITEMS_PER_RUN"

_INTEGER_RE = re.compile(r"[1-9][0-9]*\Z")


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


@dataclass(frozen=True, slots=True)
class SourceMonitoringSettings:
    enabled: bool = False
    auto_start: bool = False
    official_only: bool = True
    dry_run: bool = True
    max_items_per_run: int = MAX_OBSERVED_ITEMS_PER_POLL

    def __post_init__(self) -> None:
        for field_name in (
            "enabled",
            "auto_start",
            "official_only",
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
        if self.official_only is not True:
            raise SourceMonitoringSettingsError(
                "SOURCE_MONITORING_OFFICIAL_ONLY_REQUIRED",
                "official_only must remain true for the official source monitor",
            )
        if self.auto_start and not self.enabled:
            raise SourceMonitoringSettingsError(
                "SOURCE_MONITORING_SETTING_CONFLICT",
                "auto_start cannot be enabled while monitoring is disabled",
            )

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
            dry_run=_strict_boolean(
                source,
                SOURCE_MONITOR_DRY_RUN_ENV,
                default=True,
            ),
            max_items_per_run=_strict_max_items(source),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "auto_start": self.auto_start,
            "official_only": self.official_only,
            "dry_run": self.dry_run,
            "max_items_per_run": self.max_items_per_run,
        }


def load_source_monitoring_settings(
    environment: Mapping[str, str] | None = None,
) -> SourceMonitoringSettings:
    return SourceMonitoringSettings.from_environment(environment)


__all__ = [
    "SOURCE_MONITOR_AUTO_START_ENV",
    "SOURCE_MONITOR_DRY_RUN_ENV",
    "SOURCE_MONITOR_ENABLED_ENV",
    "SOURCE_MONITOR_MAX_ITEMS_ENV",
    "SOURCE_MONITOR_OFFICIAL_ONLY_ENV",
    "SourceMonitoringSettings",
    "SourceMonitoringSettingsError",
    "load_source_monitoring_settings",
]
