from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timezone

from backend.telegram_intelligence.dedup import (
    Deduplicator,
    canonicalize_url,
    normalize_text,
)
from backend.telegram_intelligence.models import SourceMessage
from backend.telegram_intelligence.rules import RuleEngine


NOW = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)


def message(text: str, *, chat_id: int = -1001, message_id: int = 1) -> SourceMessage:
    return SourceMessage(
        source="telegram",
        chat_id=chat_id,
        chat_title="Wire",
        chat_type="channel",
        message_id=message_id,
        sender_id=None,
        sender_name="",
        timestamp=NOW,
        edit_timestamp=None,
        text=text,
        caption="",
        urls=(),
        entities=(),
        reply_to_message_id=None,
        forward_origin=None,
        media_type="",
        media_file_id="",
        telegram_link="",
        raw_language="en",
        is_edited=False,
        is_forwarded=False,
        ingested_at=NOW,
    )


class TelegramRuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = RuleEngine(
            {
                "AI": {
                    "keywords": ["NVIDIA", "Blackwell", "人工智能"],
                    "regex": [r"GPU\s{1,8}(?:launch|shipment)"],
                    "tickers": ["NVDA", "AI"],
                },
                "Macro": {
                    "keywords": ["Federal Reserve", "美联储", "rate cut"],
                },
                "Industrial": {"keywords": ["Caterpillar"], "tickers": ["CAT"]},
            },
            ticker_whitelist={
                "NVDA": ["NVIDIA"],
                "AI": ["C3.ai"],
                "IT": [],
                "ON": ["ON Semiconductor"],
                "CAT": ["Caterpillar"],
            },
        )

    def test_keywords_are_case_insensitive_and_support_chinese_and_regex(self) -> None:
        result = self.engine.match("nViDiA GPU LAUNCH，人工智能需求上升；美联储考虑 RATE CUT。")
        self.assertEqual(set(result.categories), {"AI", "Macro"})
        self.assertIn("NVDA", result.tickers)
        self.assertTrue(any(item.startswith("AI:GPU") for item in result.regex_matches))
        self.assertTrue(any("人工智能" in item for item in result.keyword_matches))

    def test_ambiguous_tickers_require_explicit_notation_or_company_alias(self) -> None:
        ordinary = self.engine.match("AI is changing IT operations on cat videos.")
        self.assertEqual(ordinary.tickers, ())
        category_phrase = self.engine.match("AI stocks remain popular while CAT photos trend.")
        self.assertEqual(category_phrase.tickers, ())

        explicit = self.engine.match("$AI rose after C3.ai earnings; NYSE:CAT also advanced.")
        self.assertEqual(set(explicit.tickers), {"AI", "CAT"})
        aliases = self.engine.match("ON Semiconductor and Caterpillar published guidance.")
        self.assertEqual(set(aliases.tickers), {"ON", "CAT"})

    def test_unambiguous_ticker_accepts_uppercase_or_lowercase_with_context(self) -> None:
        self.assertEqual(self.engine.match("NVDA unveiled a platform.").tickers, ("NVDA",))
        self.assertEqual(self.engine.match("nvda stock rose after earnings.").tickers, ("NVDA",))
        self.assertNotIn("NVDA", self.engine.match("the word nvda is malformed prose").tickers)

    def test_regex_config_rejects_backtracking_prone_constructs(self) -> None:
        unsafe = (
            r"(a+)+$",
            r"(a|aa)+$",
            r"(a)\1",
            r"a*a*a*a*b",
            r"left.*middle.*right",
            r".*Z",
            r"a+Z",
            r"(?=secret)secret",
        )
        for expression in unsafe:
            with self.subTest(expression=expression):
                with self.assertRaisesRegex(ValueError, "regex"):
                    RuleEngine({"unsafe": {"regex": [expression]}})


class TelegramDedupTests(unittest.TestCase):
    def test_url_and_presentation_noise_are_normalized(self) -> None:
        self.assertEqual(
            canonicalize_url("HTTPS://Example.COM:443/news/?utm_source=tg&b=2&a=1#part"),
            "https://example.com/news?a=1&b=2",
        )
        self.assertEqual(normalize_text("🚨 NVIDIA：Blackwell！"), "nvidia blackwell")

    def test_exact_and_similar_cross_chat_messages_are_classified_without_dropping_raw(self) -> None:
        first = message("🚨 NVIDIA launches Blackwell GPU https://example.com/n?utm_source=tg")
        exact = message(
            "NVIDIA launches Blackwell GPU https://example.com/n",
            chat_id=-1002,
            message_id=9,
        )
        result = Deduplicator().evaluate(exact, [first])
        self.assertTrue(result.is_duplicate)
        self.assertIn(result.duplicate_reason, {"content_hash", "canonical_url", "simhash"})
        self.assertGreaterEqual(result.duplicate_score, 0.86)
        self.assertEqual(result.duplicate_of, first.identity_key)

        replay = Deduplicator().evaluate(replace(first), [first])
        self.assertEqual(replay.duplicate_reason, "identity_key")
        unrelated = Deduplicator().evaluate(
            message("Federal Reserve calendar", chat_id=-1003, message_id=10),
            [first],
        )
        self.assertFalse(unrelated.is_duplicate)


if __name__ == "__main__":
    unittest.main()
