from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, is_dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .dedup import normalize_text, simhash64, simhash_similarity


EVENT_STATUSES = frozenset({"new", "developing", "confirmed", "disputed", "resolved"})
EVIDENCE_STATUSES = frozenset(
    {
        "confirmed",
        "likely",
        "unconfirmed",
        "rumor",
        "opinion",
        "analysis",
        "false_or_misleading",
        "unknown",
    }
)


def _utc_datetime(value: Any, *, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif type(value) is str:
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise ValueError(f"{field} must be an ISO datetime") from exc
    elif type(value) in {int, float}:
        parsed = datetime.fromtimestamp(float(value), tz=timezone.utc)
    else:
        raise TypeError(f"{field} must be a datetime, ISO string, or epoch")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _content_text(message: Any) -> str:
    value = getattr(message, "content_text", None)
    if callable(value):
        value = value()
    if isinstance(value, str):
        return value.strip()
    text = str(getattr(message, "text", "") or "").strip()
    caption = str(getattr(message, "caption", "") or "").strip()
    return "\n".join(item for item in (text, caption) if item)


def _identity_key(message: Any) -> tuple[Any, ...]:
    value = getattr(message, "identity_key", None)
    if callable(value):
        value = value()
    if isinstance(value, tuple):
        return value
    return (
        str(getattr(message, "source", "telegram") or "telegram"),
        getattr(message, "chat_id", None),
        getattr(message, "message_id", None),
    )


def source_key_for_values(*, source: Any, chat_id: Any, forward_origin: Any = None) -> str:
    origin = forward_origin
    if origin:
        if is_dataclass(origin):
            origin = asdict(origin)
        if isinstance(origin, dict):
            origin_chat_id = origin.get("chat_id")
            if type(origin_chat_id) is int:
                return f"forward_chat:{origin_chat_id}"
            origin_sender_id = origin.get("sender_id")
            if type(origin_sender_id) is int:
                return f"forward_user:{origin_sender_id}"
        # Hidden names, author signatures and per-post message IDs are not
        # stable principals and therefore cannot count as independent sources.
        return "forward:hidden_or_unknown"
    return f"{source or 'telegram'}:{chat_id}"


def source_key_for_message(message: Any) -> str:
    return source_key_for_values(
        source=getattr(message, "source", "telegram"),
        chat_id=getattr(message, "chat_id", ""),
        forward_origin=getattr(message, "forward_origin", None),
    )


def _bounded_score(value: Any) -> int:
    if isinstance(value, bool) or type(value) not in {int, float}:
        raise TypeError("trust score must be numeric")
    return max(0, min(100, int(round(float(value)))))


@dataclass(frozen=True, slots=True)
class EventObservation:
    message_key: tuple[Any, ...]
    source_key: str
    source_label: str
    occurred_at: datetime
    title: str
    normalized_text: str
    simhash: int
    category: str
    tickers: tuple[str, ...]
    keywords: tuple[str, ...]
    trust_score: int
    evidence_status: str = "unknown"

    @classmethod
    def from_message(
        cls,
        message: Any,
        *,
        tickers: Iterable[str] = (),
        keywords: Iterable[str] = (),
        category: str = "",
        trust_score: int = 50,
        evidence_status: str = "unknown",
        title: str = "",
    ) -> "EventObservation":
        status = str(evidence_status or "unknown").strip().lower()
        if status not in EVIDENCE_STATUSES:
            raise ValueError("unsupported evidence status")
        content = _content_text(message)
        clean_title = str(title or "").strip()
        if not clean_title:
            clean_title = next((line.strip() for line in content.splitlines() if line.strip()), "")
        clean_title = clean_title[:300]
        normalized = normalize_text(content)
        return cls(
            message_key=_identity_key(message),
            source_key=source_key_for_message(message),
            source_label=str(getattr(message, "chat_title", "") or getattr(message, "chat_id", ""))[:200],
            occurred_at=_utc_datetime(getattr(message, "timestamp", None), field="message.timestamp"),
            title=clean_title,
            normalized_text=normalized,
            simhash=simhash64(normalized),
            category=str(category or "").strip()[:80],
            tickers=tuple(sorted({str(item).strip().upper() for item in tickers if str(item).strip()})),
            keywords=tuple(sorted({normalize_text(item) for item in keywords if normalize_text(item)})),
            trust_score=_bounded_score(trust_score),
            evidence_status=status,
        )


@dataclass(frozen=True, slots=True)
class EventState:
    event_id: str
    first_seen_at: datetime
    last_seen_at: datetime
    title: str
    category: str
    tickers: tuple[str, ...]
    keywords: tuple[str, ...]
    message_keys: tuple[tuple[Any, ...], ...]
    source_keys: tuple[str, ...]
    source_trust: tuple[tuple[str, int, str], ...]
    centroid_simhash: int
    importance_score: int = 0
    confidence_score: int = 0
    status: str = "new"

    @property
    def message_count(self) -> int:
        return len(self.message_keys)

    @property
    def source_count(self) -> int:
        return len(self.source_keys)

    @property
    def highest_trust_source(self) -> str:
        if not self.source_trust:
            return ""
        return max(self.source_trust, key=lambda row: (row[1], row[0]))[2]

    @property
    def highest_trust_score(self) -> int:
        return max((row[1] for row in self.source_trust), default=0)


@dataclass(frozen=True, slots=True)
class ClusterMatch:
    event_id: str
    score: float
    reasons: tuple[str, ...]
    created: bool


class EventClusterer:
    """Small, deterministic event aggregator for a single-node deployment."""

    def __init__(
        self,
        *,
        time_window: timedelta = timedelta(hours=6),
        similarity_threshold: float = 0.62,
    ) -> None:
        if time_window <= timedelta(0):
            raise ValueError("time_window must be positive")
        if not 0.4 <= similarity_threshold <= 1.0:
            raise ValueError("similarity_threshold must be between 0.4 and 1.0")
        self.time_window = time_window
        self.similarity_threshold = float(similarity_threshold)

    @staticmethod
    def _jaccard(left: Iterable[str], right: Iterable[str]) -> float:
        left_set, right_set = set(left), set(right)
        if not left_set or not right_set:
            return 0.0
        return len(left_set & right_set) / len(left_set | right_set)

    def similarity(self, observation: EventObservation, event: EventState) -> tuple[float, tuple[str, ...]]:
        distance = min(
            abs(observation.occurred_at - event.first_seen_at),
            abs(observation.occurred_at - event.last_seen_at),
        )
        if distance > self.time_window:
            return 0.0, ("outside_time_window",)
        ticker_score = self._jaccard(observation.tickers, event.tickers)
        keyword_score = self._jaccard(observation.keywords, event.keywords)
        text_score = simhash_similarity(observation.simhash, event.centroid_simhash)
        if observation.tickers and event.tickers and ticker_score == 0:
            return 0.0, ("ticker_conflict",)
        if ticker_score == 0 and keyword_score == 0 and text_score < 0.82:
            return 0.0, ("insufficient_semantic_overlap",)
        time_score = max(0.0, 1.0 - distance.total_seconds() / self.time_window.total_seconds())
        source_bonus = 0.08 if observation.source_key in event.source_keys else 0.0
        score = min(
            1.0,
            0.38 * text_score
            + 0.34 * ticker_score
            + 0.18 * keyword_score
            + 0.10 * time_score
            + source_bonus,
        )
        reasons = tuple(
            reason
            for condition, reason in (
                (ticker_score > 0, "ticker_overlap"),
                (keyword_score > 0, "keyword_overlap"),
                (text_score >= 0.82, "text_similarity"),
                (observation.source_key in event.source_keys, "same_origin"),
            )
            if condition
        )
        return score, reasons

    def find_match(
        self,
        observation: EventObservation,
        events: Iterable[EventState],
    ) -> ClusterMatch | None:
        best: ClusterMatch | None = None
        for event in events:
            score, reasons = self.similarity(observation, event)
            if score < self.similarity_threshold:
                continue
            candidate = ClusterMatch(event.event_id, round(score, 6), reasons, False)
            if best is None or candidate.score > best.score or (
                candidate.score == best.score and candidate.event_id < best.event_id
            ):
                best = candidate
        return best

    @staticmethod
    def _initial_status(observation: EventObservation) -> str:
        if observation.evidence_status == "false_or_misleading":
            return "disputed"
        if observation.evidence_status == "confirmed" and observation.trust_score >= 80:
            return "confirmed"
        return "new"

    def create(self, observation: EventObservation) -> EventState:
        payload = json.dumps(
            [*observation.message_key, observation.normalized_text],
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
        event_id = "event_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
        return EventState(
            event_id=event_id,
            first_seen_at=observation.occurred_at,
            last_seen_at=observation.occurred_at,
            title=observation.title,
            category=observation.category,
            tickers=observation.tickers,
            keywords=observation.keywords,
            message_keys=(observation.message_key,),
            source_keys=(observation.source_key,),
            source_trust=((observation.source_key, observation.trust_score, observation.source_label),),
            centroid_simhash=observation.simhash,
            status=self._initial_status(observation),
        )

    def merge(self, event: EventState, observation: EventObservation) -> EventState:
        if event.status not in EVENT_STATUSES:
            raise ValueError("event has an unsupported status")
        message_keys = tuple(dict.fromkeys((*event.message_keys, observation.message_key)))
        source_keys = tuple(dict.fromkeys((*event.source_keys, observation.source_key)))
        trust_by_source = {row[0]: row for row in event.source_trust}
        prior = trust_by_source.get(observation.source_key)
        if prior is None or observation.trust_score > prior[1]:
            trust_by_source[observation.source_key] = (
                observation.source_key,
                observation.trust_score,
                observation.source_label,
            )
        status = event.status
        if status != "resolved":
            if observation.evidence_status == "false_or_misleading":
                status = "disputed"
            elif observation.evidence_status == "confirmed" and observation.trust_score >= 80:
                status = "confirmed"
            elif status == "new" and len(source_keys) >= 2:
                status = "developing"
        return replace(
            event,
            first_seen_at=min(event.first_seen_at, observation.occurred_at),
            last_seen_at=max(event.last_seen_at, observation.occurred_at),
            title=event.title or observation.title,
            category=event.category or observation.category,
            tickers=tuple(sorted(set(event.tickers) | set(observation.tickers))),
            keywords=tuple(sorted(set(event.keywords) | set(observation.keywords))),
            message_keys=message_keys,
            source_keys=source_keys,
            source_trust=tuple(sorted(trust_by_source.values())),
            status=status,
        )

    def add(
        self,
        observation: EventObservation,
        events: Iterable[EventState],
    ) -> tuple[EventState, ClusterMatch]:
        event_rows = tuple(events)
        match = self.find_match(observation, event_rows)
        if match is None:
            event = self.create(observation)
            return event, ClusterMatch(event.event_id, 1.0, ("new_event",), True)
        event = next(row for row in event_rows if row.event_id == match.event_id)
        return self.merge(event, observation), match


EventAggregator = EventClusterer


__all__ = [
    "ClusterMatch",
    "EVIDENCE_STATUSES",
    "EVENT_STATUSES",
    "EventAggregator",
    "EventClusterer",
    "EventObservation",
    "EventState",
    "source_key_for_message",
    "source_key_for_values",
]
