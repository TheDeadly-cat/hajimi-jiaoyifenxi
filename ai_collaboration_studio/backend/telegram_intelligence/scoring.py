from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


def _score(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or type(value) not in {int, float}:
        raise TypeError(f"{field} must be numeric")
    return max(0, min(100, int(round(float(value)))))


@dataclass(frozen=True, slots=True)
class PreScoreConfig:
    store_only_below: int = 30
    full_analysis_above: int = 60
    base_score: int = 4
    source_priority_weight: int = 12
    low_chat_priority: int = 2
    medium_chat_priority: int = 7
    high_chat_priority: int = 13
    keyword_weight: int = 4
    keyword_cap: int = 16
    ticker_weight: int = 6
    ticker_cap: int = 18
    breaking_weight: int = 4
    url_weight: int = 5
    forward_weight: int = 2
    forward_cap: int = 6
    frequency_weight: int = 4
    frequency_cap: int = 20
    length_cap: int = 10

    def __post_init__(self) -> None:
        if not 0 <= self.store_only_below <= self.full_analysis_above <= 100:
            raise ValueError("pre-score thresholds are invalid")
        for name in (
            "base_score",
            "source_priority_weight",
            "low_chat_priority",
            "medium_chat_priority",
            "high_chat_priority",
            "keyword_weight",
            "keyword_cap",
            "ticker_weight",
            "ticker_cap",
            "breaking_weight",
            "url_weight",
            "forward_weight",
            "forward_cap",
            "frequency_weight",
            "frequency_cap",
            "length_cap",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class PreScoreResult:
    score: int
    tier: str
    components: tuple[tuple[str, int], ...]


class PreScoreEngine:
    def __init__(self, config: PreScoreConfig | None = None) -> None:
        self.config = config or PreScoreConfig()

    def score(
        self,
        *,
        source_priority: int | float = 50,
        chat_priority: str | int | float = "medium",
        keyword_matches: int = 0,
        ticker_matches: int = 0,
        breaking_keyword: bool = False,
        message_length: int = 0,
        forward_count: int = 0,
        same_event_frequency: int = 0,
        has_url: bool = False,
    ) -> PreScoreResult:
        for name, value in (
            ("keyword_matches", keyword_matches),
            ("ticker_matches", ticker_matches),
            ("message_length", message_length),
            ("forward_count", forward_count),
            ("same_event_frequency", same_event_frequency),
        ):
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if type(breaking_keyword) is not bool or type(has_url) is not bool:
            raise TypeError("pre-score flags must be booleans")
        config = self.config
        components: dict[str, int] = {"base": config.base_score}
        components["source_priority"] = round(
            _score(source_priority, field="source_priority")
            / 100
            * config.source_priority_weight
        )
        if isinstance(chat_priority, str):
            priority = chat_priority.strip().lower()
            priority_values = {
                "low": config.low_chat_priority,
                "medium": config.medium_chat_priority,
                "high": config.high_chat_priority,
            }
            if priority not in priority_values:
                raise ValueError("chat_priority must be low, medium, or high")
            components["chat_priority"] = priority_values[priority]
        else:
            components["chat_priority"] = round(_score(chat_priority, field="chat_priority") / 100 * 13)
        components["keywords"] = min(config.keyword_cap, keyword_matches * config.keyword_weight)
        components["tickers"] = min(config.ticker_cap, ticker_matches * config.ticker_weight)
        components["breaking"] = config.breaking_weight if breaking_keyword else 0
        components["urls"] = config.url_weight if has_url else 0
        components["forwards"] = min(config.forward_cap, forward_count * config.forward_weight)
        components["frequency"] = min(
            config.frequency_cap,
            max(0, same_event_frequency - 1) * config.frequency_weight,
        )
        if message_length <= 0:
            length_score = 0
        elif message_length < 20:
            length_score = 1
        elif message_length <= 1_500:
            length_score = min(config.length_cap, 3 + message_length // 180)
        else:
            length_score = max(2, config.length_cap - min(6, (message_length - 1_500) // 1_000))
        components["message_length"] = int(length_score)
        total = max(0, min(100, sum(components.values())))
        if total < config.store_only_below:
            tier = "store_only"
        elif total <= config.full_analysis_above:
            tier = "light_ai"
        else:
            tier = "full_ai"
        return PreScoreResult(total, tier, tuple(components.items()))


@dataclass(frozen=True, slots=True)
class ScoreEngineConfig:
    watch_importance: int = 45
    alert_importance: int = 70
    urgent_importance: int = 90
    alert_confidence: int = 55
    urgent_confidence: int = 75
    rumor_confidence_cap: int = 45
    unconfirmed_confidence_cap: int = 60
    opinion_confidence_cap: int = 55
    anonymous_single_source_cap: int = 35

    def __post_init__(self) -> None:
        values = (
            self.watch_importance,
            self.alert_importance,
            self.urgent_importance,
            self.alert_confidence,
            self.urgent_confidence,
            self.rumor_confidence_cap,
            self.unconfirmed_confidence_cap,
            self.opinion_confidence_cap,
            self.anonymous_single_source_cap,
        )
        if any(type(value) is not int or not 0 <= value <= 100 for value in values):
            raise ValueError("score thresholds must be integer scores")
        if not self.watch_importance <= self.alert_importance <= self.urgent_importance:
            raise ValueError("importance thresholds are invalid")
        if self.alert_confidence > self.urgent_confidence:
            raise ValueError("confidence thresholds are invalid")


@dataclass(frozen=True, slots=True)
class ScoreResult:
    importance_score: int
    confidence_score: int
    recommended_action: str
    components: Mapping[str, int]
    applied_caps: tuple[str, ...]


class ScoreEngine:
    """Combine source and corroboration evidence with, but never delegate to, AI."""

    _TYPE_CONFIDENCE = {
        "confirmed": 90,
        "likely": 70,
        "unconfirmed": 45,
        "rumor": 25,
        "opinion": 40,
        "analysis": 55,
        "false_or_misleading": 5,
        "unknown": 30,
    }
    _TYPE_IMPORTANCE = {
        "confirmed": 75,
        "likely": 65,
        "unconfirmed": 50,
        "rumor": 45,
        "opinion": 35,
        "analysis": 50,
        "false_or_misleading": 40,
        "unknown": 35,
    }

    def __init__(self, config: ScoreEngineConfig | None = None) -> None:
        self.config = config or ScoreEngineConfig()

    def score(
        self,
        *,
        source_trust: int | float,
        cross_source_count: int,
        message_type: str,
        ai_importance: int | float,
        ai_confidence: int | float,
        novelty_score: int | float = 50,
        pre_score: int | float = 50,
        anonymous_source: bool = False,
        independent_sources: int | None = None,
    ) -> ScoreResult:
        if type(cross_source_count) is not int or cross_source_count < 1:
            raise ValueError("cross_source_count must be a positive integer")
        if independent_sources is None:
            independent_sources = cross_source_count
        if type(independent_sources) is not int or not 1 <= independent_sources <= cross_source_count:
            raise ValueError("independent_sources is invalid")
        if type(anonymous_source) is not bool:
            raise TypeError("anonymous_source must be boolean")
        evidence_type = str(message_type or "unknown").strip().lower()
        if evidence_type not in self._TYPE_CONFIDENCE:
            evidence_type = "unknown"
        trust = _score(source_trust, field="source_trust")
        ai_imp = _score(ai_importance, field="ai_importance")
        ai_conf = _score(ai_confidence, field="ai_confidence")
        novelty = _score(novelty_score, field="novelty_score")
        local_pre = _score(pre_score, field="pre_score")
        corroboration = min(100, 20 + max(0, independent_sources - 1) * 22)
        type_importance = self._TYPE_IMPORTANCE[evidence_type]
        type_confidence = self._TYPE_CONFIDENCE[evidence_type]

        importance = round(
            0.35 * ai_imp
            + 0.20 * trust
            + 0.15 * corroboration
            + 0.10 * type_importance
            + 0.10 * novelty
            + 0.10 * local_pre
        )
        confidence = round(
            0.35 * ai_conf
            + 0.30 * trust
            + 0.20 * corroboration
            + 0.15 * type_confidence
        )
        caps: list[str] = []
        config = self.config
        cap_by_type = {
            "rumor": config.rumor_confidence_cap,
            "unconfirmed": config.unconfirmed_confidence_cap,
            "opinion": config.opinion_confidence_cap,
            "false_or_misleading": 15,
            "unknown": 50,
        }
        if evidence_type in cap_by_type and confidence > cap_by_type[evidence_type]:
            confidence = cap_by_type[evidence_type]
            caps.append(f"{evidence_type}_confidence_cap")
        if anonymous_source and independent_sources == 1:
            if confidence > config.anonymous_single_source_cap:
                confidence = config.anonymous_single_source_cap
                caps.append("anonymous_single_source_confidence_cap")
            if importance > 65:
                importance = 65
                caps.append("anonymous_single_source_importance_cap")
        if trust < 20 and independent_sources == 1 and confidence > 35:
            confidence = 35
            caps.append("low_trust_single_source_cap")

        importance = max(0, min(100, importance))
        confidence = max(0, min(100, confidence))
        if (
            importance >= config.urgent_importance
            and confidence >= config.urgent_confidence
            and evidence_type not in {"rumor", "unconfirmed", "opinion", "unknown"}
        ):
            action = "urgent_alert"
        elif importance >= config.alert_importance and confidence >= config.alert_confidence:
            action = "alert"
        elif importance >= config.watch_importance:
            action = "watch"
        elif importance >= 20:
            action = "store"
        else:
            action = "ignore"
        components = {
            "ai_importance": ai_imp,
            "ai_confidence": ai_conf,
            "source_trust": trust,
            "corroboration": corroboration,
            "type_importance": type_importance,
            "type_confidence": type_confidence,
            "novelty": novelty,
            "pre_score": local_pre,
        }
        return ScoreResult(importance, confidence, action, components, tuple(caps))


__all__ = [
    "PreScoreConfig",
    "PreScoreEngine",
    "PreScoreResult",
    "ScoreEngine",
    "ScoreEngineConfig",
    "ScoreResult",
]
