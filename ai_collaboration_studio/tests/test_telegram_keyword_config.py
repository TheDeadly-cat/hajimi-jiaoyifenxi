from __future__ import annotations

import unittest

from backend.telegram_intelligence.keyword_config import (
    KeywordConfigError,
    parse_keywords_config,
)
from backend.telegram_intelligence.rules import RuleEngine


YAML_CONFIG = """
groups:
  AI:
    keywords:
      - NVIDIA
      - Blackwell
    regex:
      - 'GPU\\s{1,8}shipment'
    tickers:
      - NVDA
  Macro:
    keywords:
      - Federal Reserve
    regex: []
    tickers: []
ticker_whitelist:
  NVDA:
    - NVIDIA
  AI:
    - C3.ai
ambiguous_tickers:
  - AI
  - IT
ticker_context:
  - stock
  - 股票
breaking_keywords:
  - BREAKING
  - 突发
"""


class TelegramKeywordConfigTests(unittest.TestCase):
    def test_parses_bounded_yaml_and_feeds_rule_engine(self) -> None:
        config = parse_keywords_config(YAML_CONFIG)
        engine = RuleEngine.from_config(config.rule_config)

        matched = engine.match("BREAKING: NVIDIA Blackwell GPU shipment; $NVDA shares")

        self.assertIn("AI", matched.categories)
        self.assertIn("NVDA", matched.tickers)
        self.assertEqual(config.breaking_keywords, ("BREAKING", "突发"))

    def test_json_is_accepted_but_unknown_fields_fail_closed(self) -> None:
        parsed = parse_keywords_config(
            '{"groups":{"Crypto":{"keywords":["BTC"],"regex":[],"tickers":["BTC"]}},'
            '"ticker_whitelist":{"BTC":[]},"breaking_keywords":[]}'
        )
        self.assertIn("Crypto", parsed.rule_config["groups"])

        with self.assertRaisesRegex(KeywordConfigError, "unknown"):
            parse_keywords_config(
                '{"groups":{"A":{"keywords":["x"]}},"execute_trades":true}'
            )

    def test_rejects_anchors_tabs_and_oversized_input(self) -> None:
        with self.assertRaises(KeywordConfigError):
            parse_keywords_config("groups:\n\tAI:\n    keywords:\n      - x\n")
        with self.assertRaises(KeywordConfigError):
            parse_keywords_config(
                "groups:\n  AI:\n    keywords:\n      - &secret x\n"
            )
        with self.assertRaisesRegex(KeywordConfigError, "size"):
            parse_keywords_config("x" * (128 * 1024 + 1))


if __name__ == "__main__":
    unittest.main()
