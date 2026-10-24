from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from collections.abc import Iterable
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from ..source_inbox_contracts import (
    PROJECT_SOURCE_ITEM_VERSION,
    SOURCE_IMPORT_PACKET_VERSION,
    SourceInboxContractError,
    canonicalize_source_url,
)


SOURCE_CHANNEL = "telegram_intelligence"
NO_PUBLIC_PERMALINK_URL = "https://t.me/"
MIN_HIGH_VALUE_IMPORTANCE = 70
_VERIFICATION_CAPS = {
    "confirmed": 0.80,
    "likely": 0.65,
    "unconfirmed": 0.45,
    "rumor": 0.35,
    "opinion": 0.45,
    "analysis": 0.50,
    "false_or_misleading": 0.20,
    "unknown": 0.35,
}
_DIRECTIONS = frozenset({"bullish", "bearish", "mixed", "neutral", "unknown"})
_INFORMATION_ACTIONS = frozenset(
    {"ignore", "store", "watch", "alert", "urgent_alert"}
)
_EVENT_STATUSES = frozenset(
    {"new", "developing", "confirmed", "disputed", "resolved"}
)


class SourceInboxProjectionError(ValueError):
    pass


def _text(value: Any, *, maximum: int, fallback: str = "") -> str:
    if isinstance(value, Enum):
        value = value.value
    clean = unicodedata.normalize("NFC", str(value or "")).replace("\r\n", "\n")
    clean = "".join(
        character
        for character in clean
        if ord(character) >= 32 or character in "\n\t"
    ).strip()
    if not clean:
        clean = fallback
    return clean[:maximum]


def _int_score(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or type(value) not in {int, float}:
        raise SourceInboxProjectionError(f"{field} must be numeric")
    clean = float(value)
    if not math.isfinite(clean):
        raise SourceInboxProjectionError(f"{field} must be finite")
    return max(0, min(100, int(round(clean))))


def _aware_utc(value: Any, *, field: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise SourceInboxProjectionError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _rfc3339(value: datetime) -> str:
    utc = _aware_utc(value, field="timestamp")
    base = utc.strftime("%Y-%m-%dT%H:%M:%S")
    fraction = f".{utc.microsecond:06d}".rstrip("0") if utc.microsecond else ""
    return f"{base}{fraction}Z"


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _enum_value(value: Any, *, fallback: str, maximum: int = 80) -> str:
    if isinstance(value, Enum):
        value = value.value
    return _text(value, maximum=maximum, fallback=fallback).lower()


def _choice(value: Any, allowed: frozenset[str], *, fallback: str) -> str:
    clean = _enum_value(value, fallback=fallback)
    return clean if clean in allowed else fallback


def _bounded_strings(
    values: Any,
    *,
    count: int,
    maximum: int,
    uppercase: bool = False,
) -> list[str]:
    if isinstance(values, str) or not isinstance(values, Iterable):
        return []
    output: list[str] = []
    for value in values:
        clean = _text(value, maximum=maximum)
        if uppercase:
            clean = clean.upper()
        if clean and clean not in output:
            output.append(clean)
        if len(output) >= count:
            break
    return output


def _event_identity(event: Any) -> str:
    event_id = _text(getattr(event, "event_id", ""), maximum=160)
    if event_id:
        return event_id
    basis = {
        "first_seen_at": _rfc3339(
            _aware_utc(getattr(event, "first_seen_at", None), field="first_seen_at")
        ),
        "title": _text(getattr(event, "title", ""), maximum=500),
        "message_count": int(getattr(event, "message_count", 0) or 0),
    }
    return f"event-{_sha256(basis)[:32]}"


def _public_sources(
    public_urls: Iterable[str] | None,
    *,
    content_basis: dict[str, Any],
    published_at: str,
) -> tuple[list[dict[str, Any]], bool]:
    accepted: list[str] = []
    if public_urls is not None and not isinstance(public_urls, str):
        for value in public_urls:
            if type(value) is not str or not value.strip():
                continue
            try:
                normalized = canonicalize_source_url(value)
            except SourceInboxContractError:
                continue
            lowered = normalized.lower()
            if (
                "t.me/+" in lowered
                or "telegram.me/+" in lowered
                or "t.me/joinchat/" in lowered
                or "telegram.me/joinchat/" in lowered
                or "t.me/c/" in lowered
                or "telegram.me/c/" in lowered
            ):
                continue
            if normalized not in accepted:
                accepted.append(normalized)
            if len(accepted) >= 12:
                break
    if not accepted:
        return (
            [
                {
                    "url": NO_PUBLIC_PERMALINK_URL,
                    "publisher": "Telegram account view; public permalink unavailable",
                    "source_type": "telegram_account_view",
                    "published_at": published_at,
                    "content_sha256": _sha256(
                        {
                            **content_basis,
                            "source_locator_kind": "telegram_root_placeholder",
                        }
                    ),
                }
            ],
            False,
        )
    return (
        [
            {
                "url": url,
                "publisher": "Telegram-linked public source",
                "source_type": "telegram_public_link",
                "published_at": published_at,
                "content_sha256": _sha256({**content_basis, "url": url}),
            }
            for url in accepted
        ],
        True,
    )


def _conservative_confidence(
    *,
    scored_confidence: int,
    analysis_confidence: int,
    trust_score: int,
    verification_status: str,
    has_public_url: bool,
) -> float:
    cap = _VERIFICATION_CAPS.get(verification_status, _VERIFICATION_CAPS["unknown"])
    values = [
        scored_confidence / 100,
        analysis_confidence / 100,
        min(0.80, (trust_score + 10) / 100),
        cap,
    ]
    if not has_public_url:
        values.append(0.55)
    return round(max(0.0, min(values)), 4)


def _severity(
    *,
    importance: int,
    confidence: float,
    verification_status: str,
) -> str:
    if verification_status == "false_or_misleading":
        return "low"
    if verification_status in {"rumor", "unconfirmed", "opinion", "unknown"}:
        return "medium" if importance >= 80 and confidence >= 0.30 else "low"
    if importance >= 85 and confidence >= 0.60:
        return "high"
    if importance >= 70 and confidence >= 0.40:
        return "medium"
    return "low"


def build_source_import_packet(
    event: Any,
    analysis: Any,
    score: Any | None = None,
    *,
    checked_at: datetime,
    public_urls: Iterable[str] | None = None,
    minimum_importance: int = MIN_HIGH_VALUE_IMPORTANCE,
) -> dict[str, Any]:
    """Project one high-value Telegram event into a read-only inbox packet.

    This function performs no network or storage operation.  The host Source
    Inbox contract remains responsible for validation and marks every Telegram
    and model-derived claim as external_unverified.
    """

    if type(minimum_importance) is not int or not 0 <= minimum_importance <= 100:
        raise SourceInboxProjectionError("minimum_importance must be from 0 to 100")
    first_seen = _aware_utc(
        getattr(event, "first_seen_at", None),
        field="event.first_seen_at",
    )
    last_seen = _aware_utc(
        getattr(event, "last_seen_at", None),
        field="event.last_seen_at",
    )
    checked = _aware_utc(checked_at, field="checked_at")
    if last_seen < first_seen:
        raise SourceInboxProjectionError("event time range is invalid")
    if checked < last_seen:
        raise SourceInboxProjectionError("checked_at may not precede event.last_seen_at")

    event_id = _event_identity(event)
    importance = _int_score(
        getattr(
            score,
            "importance_score",
            getattr(event, "importance_score", getattr(analysis, "importance_score", 0)),
        ),
        field="importance_score",
    )
    if importance < minimum_importance:
        raise SourceInboxProjectionError("event does not meet the high-value threshold")
    scored_confidence = _int_score(
        getattr(
            score,
            "confidence_score",
            getattr(event, "confidence_score", 0),
        ),
        field="confidence_score",
    )
    analysis_confidence = _int_score(
        getattr(analysis, "confidence_score", 0),
        field="analysis.confidence_score",
    )
    trust_score = _int_score(
        getattr(event, "highest_trust_score", 0),
        field="highest_trust_score",
    )
    verification_status = _enum_value(
        getattr(analysis, "verification_status", "unknown"),
        fallback="unknown",
    )
    if verification_status not in _VERIFICATION_CAPS:
        verification_status = "unknown"
    title = _text(
        getattr(event, "title", ""),
        maximum=500,
        fallback=_text(getattr(analysis, "summary", ""), maximum=500),
    )
    if not title:
        title = "High-value Telegram information event"
    summary = _text(
        getattr(analysis, "summary", ""),
        maximum=7_500,
        fallback=title,
    )
    message_count = max(1, int(getattr(event, "message_count", 1) or 1))
    source_count = max(1, int(getattr(event, "source_count", 1) or 1))
    message_identity_sha256 = _sha256(
        list(getattr(event, "message_keys", ()) or ())
    )
    content_basis = {
        "version": "telegram_source_inbox_projection_v1",
        "event_id": event_id,
        "first_seen_at": _rfc3339(first_seen),
        "last_seen_at": _rfc3339(last_seen),
        "title": title,
        "summary": summary,
        "message_identity_sha256": message_identity_sha256,
    }
    sources, has_public_url = _public_sources(
        public_urls,
        content_basis=content_basis,
        published_at=_rfc3339(first_seen),
    )
    confidence = _conservative_confidence(
        scored_confidence=scored_confidence,
        analysis_confidence=analysis_confidence,
        trust_score=trust_score,
        verification_status=verification_status,
        has_public_url=has_public_url,
    )
    severity = _severity(
        importance=importance,
        confidence=confidence,
        verification_status=verification_status,
    )
    extension = {
        "projection_version": "telegram_source_inbox_projection_v1",
        "event_id": event_id,
        "event_status": _choice(
            getattr(event, "status", "new"),
            _EVENT_STATUSES,
            fallback="new",
        ),
        "message_count": min(message_count, 1_000_000),
        "source_count": min(source_count, 1_000_000),
        "message_identity_sha256": message_identity_sha256,
        "category": _text(
            getattr(analysis, "category", getattr(event, "category", "")),
            maximum=80,
        ),
        "tickers": _bounded_strings(
            getattr(analysis, "tickers", getattr(event, "tickers", ())),
            count=32,
            maximum=32,
            uppercase=True,
        ),
        "direction": _choice(
            getattr(analysis, "direction", "unknown"),
            _DIRECTIONS,
            fallback="unknown",
        ),
        "sentiment": _text(getattr(analysis, "sentiment", ""), maximum=80),
        "importance_score": importance,
        "confidence_score": scored_confidence,
        "analysis_confidence_score": analysis_confidence,
        "source_trust_max": trust_score,
        "novelty_score": _int_score(
            getattr(analysis, "novelty_score", 0),
            field="analysis.novelty_score",
        ),
        "verification_status": verification_status,
        "verification_needed": bool(
            getattr(analysis, "verification_needed", True)
        ),
        "recommended_action": _choice(
            getattr(
                score,
                "recommended_action",
                getattr(analysis, "recommended_action", "watch"),
            ),
            _INFORMATION_ACTIONS,
            fallback="watch",
        ),
        "time_horizon": _text(
            getattr(analysis, "time_horizon", ""),
            maximum=160,
        ),
        "risk_flags": _bounded_strings(
            getattr(analysis, "risk_flags", ()),
            count=20,
            maximum=200,
        ),
        "public_url_available": has_public_url,
        "source_locator_kind": (
            "public_link" if has_public_url else "telegram_root_placeholder"
        ),
    }
    extension["projection_sha256"] = _sha256(extension)
    external_item_id = f"telegram-event-{_sha256({'event_id': event_id})[:32]}"
    all_source_indexes = list(range(len(sources)))
    unknowns = [
        "Telegram and model-derived claims have not been independently verified by the host.",
    ]
    if not has_public_url:
        unknowns.append(
            "No public permalink is available; review requires the authorized Telegram account."
        )
    usage = getattr(analysis, "usage", None)
    model = _text(getattr(usage, "model", ""), maximum=160)
    run_basis = {
        "event_id": event_id,
        "checked_at": _rfc3339(checked),
        "projection_sha256": extension["projection_sha256"],
    }
    return {
        "version": SOURCE_IMPORT_PACKET_VERSION,
        "source_channel": SOURCE_CHANNEL,
        "source_key": f"telegram-event-{_sha256({'event_id': event_id})[:32]}",
        "external_run_id": f"telegram-projection-{_sha256(run_basis)[:32]}",
        "checked_at": _rfc3339(checked),
        "cutoff_at": _rfc3339(last_seen),
        "meaningful_change": True,
        "items": [
            {
                "version": PROJECT_SOURCE_ITEM_VERSION,
                "external_item_id": external_item_id,
                "item_type": "external_information_event",
                "severity": severity,
                "occurred_at": _rfc3339(first_seen),
                "published_at": _rfc3339(first_seen),
                "entities": [
                    {
                        "kind": "information_event",
                        "id": external_item_id,
                        "label": title[:240],
                    }
                ],
                "headline": title,
                "summary": summary,
                "facts": [
                    {
                        "claim": (
                            f"Telegram aggregation observed {message_count} message(s) "
                            f"across {source_count} source label(s)."
                        ),
                        "source_indexes": all_source_indexes,
                    },
                    {
                        "claim": summary[:4_000],
                        "source_indexes": all_source_indexes,
                    },
                ],
                "sources": sources,
                "impact_hypotheses": [],
                "unknowns": unknowns,
                "confidence": confidence,
                "recommended_route": "notify_only",
                "extensions": {"telegram_v1": extension},
            }
        ],
        "generation": {
            "channel": SOURCE_CHANNEL,
            "model": model,
            "cost": {
                "status": "unavailable",
                "amount": None,
                "currency": "",
                "usage_source": "telegram_projection_unavailable",
            },
            "correlated_output": True,
        },
    }


__all__ = [
    "MIN_HIGH_VALUE_IMPORTANCE",
    "NO_PUBLIC_PERMALINK_URL",
    "SOURCE_CHANNEL",
    "SourceInboxProjectionError",
    "build_source_import_packet",
]
