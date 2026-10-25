from __future__ import annotations

import unittest

from backend.telegram_intelligence.scoring import PreScoreEngine, ScoreEngine


class TelegramPreScoreTests(unittest.TestCase):
    def test_configurable_funnel_returns_store_light_and_full_tiers(self) -> None:
        engine = PreScoreEngine()
        stored = engine.score(
            source_priority=10,
            chat_priority="low",
            message_length=8,
        )
        light = engine.score(
            source_priority=50,
            chat_priority="medium",
            keyword_matches=2,
            ticker_matches=1,
            message_length=300,
            has_url=True,
        )
        full = engine.score(
            source_priority=95,
            chat_priority="high",
            keyword_matches=4,
            ticker_matches=3,
            breaking_keyword=True,
            message_length=800,
            forward_count=3,
            same_event_frequency=5,
            has_url=True,
        )
        self.assertEqual(stored.tier, "store_only")
        self.assertEqual(light.tier, "light_ai")
        self.assertEqual(full.tier, "full_ai")
        self.assertLess(stored.score, light.score)
        self.assertLess(light.score, full.score)
        self.assertLessEqual(full.score, 100)

    def test_breaking_language_has_small_bounded_effect(self) -> None:
        engine = PreScoreEngine()
        ordinary = engine.score(message_length=100)
        breaking = engine.score(message_length=100, breaking_keyword=True)
        self.assertEqual(breaking.score - ordinary.score, 4)


class TelegramScoreEngineTests(unittest.TestCase):
    def test_high_ai_scores_alone_cannot_create_an_alert(self) -> None:
        result = ScoreEngine().score(
            source_trust=0,
            cross_source_count=1,
            independent_sources=1,
            message_type="unknown",
            ai_importance=100,
            ai_confidence=100,
            novelty_score=50,
            pre_score=50,
            anonymous_source=True,
        )
        self.assertLessEqual(result.importance_score, 65)
        self.assertLessEqual(result.confidence_score, 35)
        self.assertNotIn(result.recommended_action, {"alert", "urgent_alert"})
        self.assertIn("anonymous_single_source_confidence_cap", result.applied_caps)

    def test_confirmed_trusted_independent_sources_can_reach_urgent_alert(self) -> None:
        result = ScoreEngine().score(
            source_trust=100,
            cross_source_count=4,
            independent_sources=4,
            message_type="confirmed",
            ai_importance=100,
            ai_confidence=100,
            novelty_score=100,
            pre_score=100,
        )
        self.assertGreaterEqual(result.importance_score, 90)
        self.assertGreaterEqual(result.confidence_score, 75)
        self.assertEqual(result.recommended_action, "urgent_alert")

    def test_rumor_and_unconfirmed_confidence_are_capped(self) -> None:
        rumor = ScoreEngine().score(
            source_trust=100,
            cross_source_count=5,
            independent_sources=5,
            message_type="rumor",
            ai_importance=100,
            ai_confidence=100,
            novelty_score=100,
            pre_score=100,
        )
        unconfirmed = ScoreEngine().score(
            source_trust=100,
            cross_source_count=5,
            independent_sources=5,
            message_type="unconfirmed",
            ai_importance=100,
            ai_confidence=100,
            novelty_score=100,
            pre_score=100,
        )
        self.assertEqual(rumor.confidence_score, 45)
        self.assertEqual(unconfirmed.confidence_score, 60)
        self.assertEqual(rumor.recommended_action, "watch")
        self.assertIn("rumor_confidence_cap", rumor.applied_caps)

    def test_forward_count_is_not_mistaken_for_independent_corroboration(self) -> None:
        forwarded = ScoreEngine().score(
            source_trust=70,
            cross_source_count=8,
            independent_sources=1,
            message_type="likely",
            ai_importance=70,
            ai_confidence=70,
        )
        independent = ScoreEngine().score(
            source_trust=70,
            cross_source_count=8,
            independent_sources=8,
            message_type="likely",
            ai_importance=70,
            ai_confidence=70,
        )
        self.assertLess(forwarded.confidence_score, independent.confidence_score)
        self.assertLess(forwarded.importance_score, independent.importance_score)


if __name__ == "__main__":
    unittest.main()
