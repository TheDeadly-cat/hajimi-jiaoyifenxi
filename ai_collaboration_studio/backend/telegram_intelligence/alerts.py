from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class AlertAction(str, Enum):
    ALERT = "alert"
    UPDATE = "update"
    URGENT_UPDATE = "urgent_update"
    SUPPRESS = "suppress"


@dataclass(frozen=True, slots=True)
class AlertDecision:
    action: AlertAction
    reason: str

    @property
    def should_send(self) -> bool:
        return self.action is not AlertAction.SUPPRESS

    @property
    def is_update(self) -> bool:
        return self.action in {AlertAction.UPDATE, AlertAction.URGENT_UPDATE}

    @property
    def urgent(self) -> bool:
        return self.action is AlertAction.URGENT_UPDATE

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "reason": self.reason,
            "should_send": self.should_send,
            "is_update": self.is_update,
            "urgent": self.urgent,
        }


def _text(value: Any) -> str:
    return str(value or "").strip().lower()


def _trust_score(event: Mapping[str, Any]) -> int:
    for key in ("highest_trust_score", "source_trust", "trust_score"):
        value = event.get(key)
        if type(value) is bool:
            continue
        try:
            return max(0, min(100, int(round(float(value)))))
        except (TypeError, ValueError, OverflowError):
            continue
    return 0


def _official_denial(event: Mapping[str, Any]) -> bool:
    # This bypass is reserved for a deterministic host verifier.  Model-owned
    # event_type/risk_flags are claims and cannot establish an official denial.
    return type(event.get("official_denial")) is bool and event["official_denial"]


def _aware_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)) and type(value) is not bool:
        try:
            parsed = datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, str) and value.strip():
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


class AlertCooldown:
    """Pure same-event alert policy; callers persist the previous snapshot."""

    def __init__(self, *, window_seconds: int = 300) -> None:
        if type(window_seconds) is bool or int(window_seconds) < 1:
            raise ValueError("window_seconds must be a positive integer")
        self.window_seconds = min(86_400, int(window_seconds))

    def decide(
        self,
        previous: Mapping[str, Any] | None,
        current: Mapping[str, Any],
        *,
        now: datetime | None = None,
    ) -> AlertDecision:
        if not isinstance(current, Mapping):
            raise TypeError("current must be a mapping")
        if previous is not None and not isinstance(previous, Mapping):
            raise TypeError("previous must be a mapping when provided")
        current_time = now or datetime.now(timezone.utc)
        if not isinstance(current_time, datetime):
            raise TypeError("now must be a datetime when provided")
        if current_time.tzinfo is None or current_time.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        current_time = current_time.astimezone(timezone.utc)

        if not previous:
            return AlertDecision(AlertAction.ALERT, "first_event_alert")
        previous_event_id = str(previous.get("event_id") or "").strip()
        current_event_id = str(current.get("event_id") or "").strip()
        if previous_event_id and current_event_id and previous_event_id != current_event_id:
            return AlertDecision(AlertAction.ALERT, "first_event_alert")

        if _official_denial(current) and not _official_denial(previous):
            return AlertDecision(AlertAction.URGENT_UPDATE, "official_denial")

        previous_direction = _text(previous.get("direction"))
        current_direction = _text(current.get("direction"))
        valid_directions = {"bullish", "bearish", "mixed", "neutral"}
        if (
            current_direction in valid_directions
            and previous_direction != current_direction
        ):
            return AlertDecision(AlertAction.UPDATE, "direction_changed")

        current_status = _text(current.get("verification_status") or current.get("status"))
        previous_trust = _trust_score(previous)
        current_trust = _trust_score(current)
        if current_status == "confirmed" and current_trust > previous_trust:
            return AlertDecision(AlertAction.UPDATE, "higher_trust_confirmation")

        last_alert_at = _aware_datetime(
            previous.get("last_alert_at") or previous.get("sent_at")
        )
        if last_alert_at is None:
            return AlertDecision(AlertAction.SUPPRESS, "previous_alert_time_invalid")
        elapsed = (current_time - last_alert_at).total_seconds()
        if elapsed < 0:
            return AlertDecision(AlertAction.SUPPRESS, "clock_skew")
        if elapsed < self.window_seconds:
            return AlertDecision(AlertAction.SUPPRESS, "ordinary_duplicate_within_cooldown")
        return AlertDecision(AlertAction.ALERT, "cooldown_elapsed")
