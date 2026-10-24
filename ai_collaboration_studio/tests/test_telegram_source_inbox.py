from __future__ import annotations

import copy
import unittest
from datetime import datetime, timedelta, timezone

from backend.source_inbox_contracts import (
    EXTERNAL_UNVERIFIED,
    normalize_source_import_packet,
)
from backend.telegram_intelligence.analyzer import (
    AnalysisResult,
    AnalysisUsage,
    Direction,
    RecommendedAction,
    VerificationStatus,
)
from backend.telegram_intelligence.events import EventState
from backend.telegram_intelligence.scoring import ScoreResult
from backend.telegram_intelligence.source_inbox import (
    NO_PUBLIC_PERMALINK_URL,
    SourceInboxProjectionError,
    build_source_import_packet,
)


FIRST_SEEN = datetime(2026, 8, 29, 4, 0, tzinfo=timezone.utc)
LAST_SEEN = FIRST_SEEN + timedelta(minutes=3)
CHECKED_AT = LAST_SEEN + timedelta(seconds=15)


def _event(
    *,
    importance: int = 92,
    confidence: int = 82,
    trust: int = 95,
) -> EventState:
    return EventState(
        event_id="evt_telegram_nvda_20260829",
        first_seen_at=FIRST_SEEN,
        last_seen_at=LAST_SEEN,
        title="NVIDIA announces a new accelerator",
        category="ai_hardware",
        tickers=("NVDA",),
        keywords=("accelerator", "nvidia"),
        message_keys=(
            ("telegram", -100123, 1001),
            ("telegram", -100456, 2002),
        ),
        source_keys=("telegram:-100123", "telegram:-100456"),
        source_trust=(
            ("telegram:-100123", trust, "Official desk"),
            ("telegram:-100456", 70, "Market desk"),
        ),
        centroid_simhash=123456,
        importance_score=importance,
        confidence_score=confidence,
        status="developing",
    )


def _analysis(
    *,
    verification: VerificationStatus = VerificationStatus.LIKELY,
    confidence: int = 84,
) -> AnalysisResult:
    return AnalysisResult(
        event_type="product_announcement",
        category="ai_hardware",
        tickers=("NVDA",),
        entities=("NVIDIA",),
        summary="Multiple Telegram sources describe a new NVIDIA accelerator.",
        sentiment="positive",
        direction=Direction.BULLISH,
        importance_score=94,
        confidence_score=confidence,
        novelty_score=88,
        verification_needed=True,
        verification_status=verification,
        time_horizon="days",
        bull_case="Demand may strengthen.",
        bear_case="Details may be incomplete.",
        risk_flags=("No independent primary-source verification",),
        recommended_action=RecommendedAction.ALERT,
        usage=AnalysisUsage(
            provider="openai",
            model="gpt-test-model",
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
        ),
    )


def _score(
    *,
    importance: int = 92,
    confidence: int = 78,
    action: str = "alert",
) -> ScoreResult:
    return ScoreResult(
        importance_score=importance,
        confidence_score=confidence,
        recommended_action=action,
        components={"source_trust": 40},
        applied_caps=(),
    )


def _normalize(packet):
    return normalize_source_import_packet(
        packet,
        received_at_ms=int(CHECKED_AT.timestamp() * 1_000),
    )


class TelegramSourceInboxProjectionTests(unittest.TestCase):
    def test_public_event_normalizes_and_remains_external_unverified(self) -> None:
        packet = build_source_import_packet(
            _event(),
            _analysis(),
            _score(),
            checked_at=CHECKED_AT,
            public_urls=["https://t.me/nvidia_news/123#ignored-fragment"],
        )
        normalized = _normalize(packet)
        item = normalized["items"][0]
        self.assertEqual(
            normalized["external_claims_verification"]["model"],
            EXTERNAL_UNVERIFIED,
        )
        self.assertEqual(item["external_claims_verification"], EXTERNAL_UNVERIFIED)
        self.assertEqual(normalized["safety"]["network_requests_performed"], 0)
        self.assertEqual(normalized["safety"]["provider_calls_performed"], 0)
        self.assertEqual(item["sources"][0]["url"], "https://t.me/nvidia_news/123")
        self.assertNotEqual(item["severity"], "critical")
        self.assertLessEqual(item["confidence"], 0.65)
        self.assertEqual(item["recommended_route"], "notify_only")

        extension = item["extensions"]["telegram_v1"]
        self.assertEqual(extension["tickers"], ["NVDA"])
        self.assertEqual(extension["direction"], "bullish")
        self.assertTrue(extension["public_url_available"])
        self.assertEqual(
            {entity["kind"] for entity in item["entities"]},
            {"information_event"},
        )
        self.assertNotIn("tickers", item)
        self.assertNotIn("direction", item)

    def test_missing_or_private_invite_url_uses_honest_public_placeholder(self) -> None:
        for urls in (
            None,
            [],
            ["https://t.me/+privateInviteValue"],
            ["https://t.me/c/123456/789"],
        ):
            with self.subTest(urls=urls):
                packet = build_source_import_packet(
                    _event(),
                    _analysis(),
                    _score(),
                    checked_at=CHECKED_AT,
                    public_urls=urls,
                )
                normalized = _normalize(packet)
                item = normalized["items"][0]
                source = item["sources"][0]
                extension = item["extensions"]["telegram_v1"]
                self.assertEqual(source["url"], NO_PUBLIC_PERMALINK_URL)
                self.assertIn("public permalink unavailable", source["publisher"])
                self.assertFalse(extension["public_url_available"])
                self.assertEqual(
                    extension["source_locator_kind"],
                    "telegram_root_placeholder",
                )
                self.assertTrue(
                    any("authorized Telegram account" in value for value in item["unknowns"])
                )

    def test_projection_ids_and_hashes_are_deterministic(self) -> None:
        first = build_source_import_packet(
            _event(),
            _analysis(),
            _score(),
            checked_at=CHECKED_AT,
        )
        second = build_source_import_packet(
            copy.deepcopy(_event()),
            copy.deepcopy(_analysis()),
            copy.deepcopy(_score()),
            checked_at=CHECKED_AT,
        )
        self.assertEqual(first, second)
        first_normalized = _normalize(first)
        second_normalized = _normalize(second)
        self.assertEqual(
            first_normalized["items"][0]["server_fingerprint"],
            second_normalized["items"][0]["server_fingerprint"],
        )
        self.assertEqual(
            len(first["items"][0]["sources"][0]["content_sha256"]),
            64,
        )
        self.assertEqual(
            len(first["items"][0]["extensions"]["telegram_v1"]["projection_sha256"]),
            64,
        )

    def test_rumor_confidence_and_severity_are_conservatively_capped(self) -> None:
        packet = build_source_import_packet(
            _event(confidence=99, trust=99),
            _analysis(
                verification=VerificationStatus.RUMOR,
                confidence=99,
            ),
            _score(importance=98, confidence=99, action="urgent_alert"),
            checked_at=CHECKED_AT,
        )
        item = _normalize(packet)["items"][0]
        self.assertLessEqual(item["confidence"], 0.35)
        self.assertNotIn(item["severity"], {"high", "critical"})
        self.assertEqual(
            item["extensions"]["telegram_v1"]["verification_status"],
            "rumor",
        )

    def test_low_value_and_invalid_time_are_rejected_before_projection(self) -> None:
        with self.assertRaises(SourceInboxProjectionError):
            build_source_import_packet(
                _event(importance=40),
                _analysis(),
                _score(importance=40),
                checked_at=CHECKED_AT,
            )
        with self.assertRaises(SourceInboxProjectionError):
            build_source_import_packet(
                _event(),
                _analysis(),
                _score(),
                checked_at=LAST_SEEN - timedelta(seconds=1),
            )

    def test_execution_like_recommendation_is_not_projected(self) -> None:
        packet = build_source_import_packet(
            _event(),
            _analysis(),
            _score(action="buy"),
            checked_at=CHECKED_AT,
        )
        extension = _normalize(packet)["items"][0]["extensions"]["telegram_v1"]
        self.assertEqual(extension["recommended_action"], "watch")


if __name__ == "__main__":
    unittest.main()
